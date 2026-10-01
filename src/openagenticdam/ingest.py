"""Fast ingestion pipeline for the S3 adapter (v0.1).

hash -> EXIF/dimensions -> dominant colours -> WebP thumbnail -> text embedding.

v0.1 limitation, stated plainly: the embedding is computed from the asset's *text*
(file name, title, tags, colour names) with a text model, not from pixels. Image-content
search needs a multimodal embedding model (SigLIP-2 class), which is a v0.2 model decision.
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import uuid
from datetime import datetime

import psycopg
from PIL import ExifTags, Image

from openagenticdam.config import Settings
from openagenticdam.embeddings import embed_text
from openagenticdam.imageformat import sniff
from openagenticdam.metadata import AI_SOURCE_TYPES, ai_source_type, extract
from openagenticdam.renditions import Rendition, make_renditions, to_rgb, upright
from openagenticdam.storage import s3_client

_COLOUR_NAMES = {
    "red": (220, 30, 30),
    "green": (40, 160, 60),
    "blue": (40, 80, 200),
    "yellow": (230, 210, 40),
    "orange": (240, 140, 30),
    "purple": (130, 50, 160),
    "pink": (240, 150, 190),
    "brown": (120, 80, 40),
    "black": (15, 15, 15),
    "white": (240, 240, 240),
    "grey": (128, 128, 128),
}


def _colour_name(rgb: tuple[int, int, int]) -> str:
    return min(_COLOUR_NAMES, key=lambda n: sum((a - b) ** 2 for a, b in zip(rgb, _COLOUR_NAMES[n], strict=True)))


def _dominant_colours(img: Image.Image, n: int = 3) -> list[dict[str, object]]:
    small = img.convert("RGB").resize((64, 64))
    pal = small.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    palette = pal.getpalette() or []
    counts = sorted(pal.getcolors() or [], key=lambda c: c[0], reverse=True)
    out = []
    for count, idx in counts[:n]:
        if not isinstance(idx, int):  # P-mode images yield integer palette indices
            raise TypeError(f"unexpected colour entry {idx!r} for a palette image")
        rgb = (palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2])
        out.append(
            {"hex": "#{:02x}{:02x}{:02x}".format(*rgb), "name": _colour_name(rgb), "share": round(count / 4096, 3)}
        )
    return out


_EXIF_IFD = 0x8769
_EXIF_DATETIME_ORIGINAL = 0x9003
_EXIF_DATETIME = 0x0132


def captured_at(img: Image.Image) -> str | None:
    """Capture time from EXIF (DateTimeOriginal, else DateTime) as naive ISO 8601.

    EXIF stores local camera time without a zone, so the value stays naive on purpose.
    """
    exif = img.getexif()
    for raw in (exif.get_ifd(_EXIF_IFD).get(_EXIF_DATETIME_ORIGINAL), exif.get(_EXIF_DATETIME)):
        if not raw:
            continue
        try:
            return datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            continue
    return None


def _exif(img: Image.Image) -> dict[str, str]:
    raw = img.getexif()
    keep = {"Make", "Model", "DateTime", "Artist", "Copyright", "Software"}
    return {ExifTags.TAGS.get(k, str(k)): str(v)[:200] for k, v in raw.items() if ExifTags.TAGS.get(k) in keep}


def ensure_source(conn: psycopg.Connection, tenant: uuid.UUID, *, kind: str, name: str, bucket: str) -> uuid.UUID:
    row = conn.execute(
        "SELECT id FROM sources WHERE tenant_id = %s AND kind = %s AND config->>'bucket' = %s",
        (tenant, kind, bucket),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "INSERT INTO sources (tenant_id, kind, name, config, sync_status) VALUES (%s,%s,%s,%s,'ok') RETURNING id",
        (tenant, kind, name, json.dumps({"bucket": bucket})),
    ).fetchone()
    assert row is not None
    return row[0]


def ingest_s3_object(
    conn: psycopg.Connection,
    settings: Settings,
    tenant: uuid.UUID,
    source_id: uuid.UUID,
    key: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    acl: list[str],
    write_acl: list[str] | None = None,
) -> uuid.UUID:
    """Ingest one object. `acl` holds source-local principals; empty list = invisible (fail closed).

    `write_acl` grants change/delete rights in OpenAgenticDAM. Default: nobody - assets synced
    from a source system are managed there, not here.
    """
    obj = s3_client(settings).get_object(Bucket=settings.s3_bucket, Key=key)
    data = obj["Body"].read()
    file_name = key.rsplit("/", 1)[-1]
    mime = obj.get("ContentType") or mimetypes.guess_type(file_name)[0] or "application/octet-stream"
    modified = obj.get("LastModified")
    return ingest_bytes(
        conn,
        settings,
        tenant,
        source_id,
        key,
        data,
        mime,
        file_name=file_name,
        title=title,
        tags=tags,
        acl=acl,
        write_acl=write_acl,
        file_modified_at=modified.isoformat() if modified else None,
    )


def ingest_bytes(
    conn: psycopg.Connection,
    settings: Settings,
    tenant: uuid.UUID,
    source_id: uuid.UUID,
    key: str,
    data: bytes,
    mime: str,
    *,
    file_name: str,
    title: str | None = None,
    tags: list[str] | None = None,
    acl: list[str],
    write_acl: list[str] | None = None,
    file_modified_at: str | None = None,
) -> uuid.UUID:
    """Index bytes that already live in object storage under `key` (S3 sync or chat upload).

    `file_modified_at` (ISO 8601, timezone-aware) is the file's own modification time - the
    fallback creation date when the image has no EXIF capture time.
    """
    # Everything ExifTool can read, before anything is written: a broken extractor fails the ingest.
    meta = extract(data, settings.exiftool)
    s3 = s3_client(settings)
    # The bytes decide: object stores often say binary/octet-stream for iPhone HEICs, and a
    # Content-Type claiming "image/..." does not make garbage an image.
    detected = sniff(data)
    if detected:
        display_format, mime = detected
    elif mime.startswith("image/"):
        mime = "application/octet-stream"
    tech: dict[str, object] = {"file_modified_at": file_modified_at, "captured_at": None}
    colours: list[dict[str, object]] = []
    renditions: list[Rendition] = []

    if detected:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            shown = upright(img)  # dimensions as a viewer displays them (EXIF orientation applied)
            tech.update(
                width=shown.width,
                height=shown.height,
                format=display_format,
                exif=_exif(img),
                captured_at=captured_at(img),
            )
            colours = _dominant_colours(to_rgb(shown))
        tech["colours"] = colours
        renditions = make_renditions(data)

    # ExifTool knows zone and sub-seconds (OffsetTimeOriginal, SubSecTimeOriginal); Pillow does not.
    if meta.captured_at:
        tech["captured_at"] = meta.captured_at
    tech["capture"] = meta.capture
    tech["privacy"] = meta.privacy  # flags only - the values live in asset_sensitive_metadata
    tech["ai_source_type"] = ai_source_type(meta.general)

    row = conn.execute(
        """
        INSERT INTO assets (tenant_id, source_id, external_id, hash_sha256, file_name, mime_type,
                            file_size_bytes, title, tags, technical_metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_id, external_id) DO UPDATE SET
          hash_sha256 = EXCLUDED.hash_sha256, file_size_bytes = EXCLUDED.file_size_bytes,
          mime_type = EXCLUDED.mime_type, title = EXCLUDED.title, tags = EXCLUDED.tags,
          technical_metadata = EXCLUDED.technical_metadata, updated_at = now(), deleted_at = NULL
        RETURNING id
        """,
        (
            tenant,
            source_id,
            key,
            hashlib.sha256(data).hexdigest(),
            file_name,
            mime,
            len(data),
            title,
            tags or [],
            json.dumps(tech),
        ),
    ).fetchone()
    assert row is not None
    asset_id: uuid.UUID = row[0]

    if renditions:
        old = conn.execute(
            "SELECT storage_path FROM asset_versions WHERE asset_id = %s AND kind IN ('thumbnail', 'rendition')",
            (asset_id,),
        ).fetchall()
        conn.execute(
            "DELETE FROM asset_versions WHERE asset_id = %s AND kind IN ('thumbnail', 'rendition')", (asset_id,)
        )
        for r in renditions:
            r_key = f"renditions/{asset_id}/{r.name}.{r.ext}"
            s3.put_object(Bucket=settings.s3_bucket, Key=r_key, Body=r.data, ContentType=r.mime_type)
            conn.execute(
                "INSERT INTO asset_versions (tenant_id, asset_id, kind, storage_path, params) VALUES (%s,%s,%s,%s,%s)",
                (
                    tenant,
                    asset_id,
                    "thumbnail" if r.name == "thumbnail" else "rendition",
                    r_key,
                    json.dumps(
                        {
                            "rendition": r.name,
                            "format": r.spec.format,
                            "mime_type": r.mime_type,
                            "width": r.width,
                            "height": r.height,
                            "max_px": r.spec.max_px,
                            "quality": r.spec.quality,
                            "bytes": len(r.data),
                            "color_managed": r.color_managed,
                        }
                    ),
                ),
            )
        fresh = {f"renditions/{asset_id}/{r.name}.{r.ext}" for r in renditions}
        for (path,) in old:  # e.g. the WebP thumbnails of earlier versions
            if path not in fresh:
                s3.delete_object(Bucket=settings.s3_bucket, Key=path)

    conn.execute(
        """
        INSERT INTO asset_metadata (tenant_id, asset_id, tool, data) VALUES (%s,%s,%s,%s)
        ON CONFLICT (asset_id) DO UPDATE SET tool = EXCLUDED.tool, data = EXCLUDED.data, extracted_at = now()
        """,
        (tenant, asset_id, meta.tool, json.dumps(meta.general)),
    )
    conn.execute("DELETE FROM asset_sensitive_metadata WHERE asset_id = %s", (asset_id,))
    if meta.sensitive:
        conn.execute(
            """
            INSERT INTO asset_sensitive_metadata (tenant_id, asset_id, latitude, longitude, altitude_m,
                                                  direction_deg, accuracy_m, face_count, data)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                tenant,
                asset_id,
                meta.latitude,
                meta.longitude,
                meta.altitude_m,
                meta.direction_deg,
                meta.accuracy_m,
                meta.face_count,
                json.dumps(meta.sensitive),
            ),
        )
    if tech["ai_source_type"] is not None:
        conn.execute(
            """
            INSERT INTO asset_rights (tenant_id, asset_id, ai_generated) VALUES (%s,%s,%s)
            ON CONFLICT (asset_id) DO UPDATE SET ai_generated = EXCLUDED.ai_generated
            """,
            (tenant, asset_id, tech["ai_source_type"] in AI_SOURCE_TYPES),
        )

    # ACL is replaced, not merged: the source system is the source of truth.
    conn.execute("DELETE FROM asset_acl WHERE asset_id = %s", (asset_id,))
    grants = [(p, "read") for p in acl] + [(p, "write") for p in (write_acl or [])]
    for principal, permission in grants:
        conn.execute(
            "INSERT INTO asset_acl (tenant_id, asset_id, source_id, principal, permission) VALUES (%s,%s,%s,%s,%s)",
            (tenant, asset_id, source_id, principal, permission),
        )

    text = " ".join(
        filter(
            None,
            [
                title,
                meta.search_text,
                file_name.rsplit(".", 1)[0].replace("_", " ").replace("-", " "),
                " ".join(tags or []),
                " ".join(str(c["name"]) for c in colours),
            ],
        )
    )
    vec = embed_text(settings, text)
    conn.execute(
        """
        INSERT INTO embeddings (tenant_id, asset_id, segment, model, embedding) VALUES (%s,%s,'full',%s,%s)
        ON CONFLICT (asset_id, segment, model) DO UPDATE SET embedding = EXCLUDED.embedding
        """,
        (tenant, asset_id, settings.embed_model, str(vec)),
    )
    return asset_id


# Sources whose originals live in OpenAgenticDAM's own storage (deleting the asset deletes them).
OWNED_SOURCE_KINDS = frozenset({"upload"})


def delete_asset_rows(conn: psycopg.Connection, tenant: uuid.UUID, ids: list[uuid.UUID]) -> list[str]:
    """Delete assets (rights, ACL, versions, embeddings, metadata cascade). Returns the object keys
    to remove after commit: renditions always, originals only where we own them."""
    keys = [
        r[0]
        for r in conn.execute(
            """
            SELECT v.storage_path FROM asset_versions v JOIN assets a ON a.id = v.asset_id
            WHERE a.tenant_id = %(t)s AND a.id = ANY(%(ids)s)
            UNION ALL
            SELECT a.external_id FROM assets a JOIN sources s ON s.id = a.source_id
            WHERE a.tenant_id = %(t)s AND a.id = ANY(%(ids)s) AND s.kind = ANY(%(owned)s)
            """,
            {"t": tenant, "ids": ids, "owned": list(OWNED_SOURCE_KINDS)},
        ).fetchall()
    ]
    conn.execute("DELETE FROM assets WHERE tenant_id = %s AND id = ANY(%s)", (tenant, ids))
    return keys
