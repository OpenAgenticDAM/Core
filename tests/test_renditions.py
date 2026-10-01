"""Originals are stored byte-for-byte (incl. HEIF/HEIC); the server derives JPEG renditions."""

from __future__ import annotations

import hashlib
import io

import pillow_heif
from mcp import Client
from PIL import Image

from openagenticdam.db import connect
from openagenticdam.renditions import RENDITION_SPECS
from openagenticdam.server import build_server
from openagenticdam.storage import s3_client
from tests.test_upload import _upload, settings, tenant  # noqa: F401 - shared fixtures

pillow_heif.register_heif_opener()

ORIENTATION = 0x0112
DATETIME_ORIGINAL = 0x9003
EXIF_IFD = 0x8769


def _heic(size=(2400, 1600), rotate_tag: int | None = None, taken: str | None = None) -> bytes:
    img = Image.effect_noise(size, 60).convert("RGB")
    exif = Image.Exif()
    if rotate_tag:
        exif[ORIENTATION] = rotate_tag
    if taken:
        exif.get_ifd(EXIF_IFD)[DATETIME_ORIGINAL] = taken
    buf = io.BytesIO()
    img.save(buf, format="HEIF", quality=80, exif=exif.tobytes())
    return buf.getvalue()


def _versions(settings, asset_id: str) -> dict[str, tuple[str, dict]]:  # noqa: F811
    with connect(settings) as conn:
        rows = conn.execute(
            "SELECT kind, storage_path, params FROM asset_versions WHERE asset_id = %s", (asset_id,)
        ).fetchall()
    out: dict[str, tuple[str, dict]] = {}
    for kind, path, params in rows:
        name = params.get("rendition", kind) if params else kind
        out[name] = (path, params or {})
    return out


def _get(settings, key: str) -> bytes:  # noqa: F811
    return s3_client(settings).get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()


async def test_heic_upload_keeps_original_bytes_and_creates_renditions(settings, tenant):  # noqa: F811
    data = _heic(taken="2026:08:02 07:15:00")
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(client, "IMG_1234.HEIC", data, title="Sonnenaufgang Grossglockner")
        assert not res.is_error, res.content
        out = res.structured_content
        info = out["file_info"]
        assert info["format"] == "HEIC"  # brand "heic", like iPhones write it
        assert info["mime_type"] == "image/heic"
        assert (info["width"], info["height"]) == (2400, 1600)
        assert info["created_at"] == "2026-08-02T07:15:00"
        aid = out["asset_id"]

        # original is stored unchanged
        with connect(settings) as conn:
            key, size, sha = conn.execute(
                "SELECT external_id, file_size_bytes, hash_sha256 FROM assets WHERE id = %s", (aid,)
            ).fetchone()
        assert key.endswith("/IMG_1234.HEIC")
        assert _get(settings, key) == data
        assert size == len(data) and sha == hashlib.sha256(data).hexdigest()

        # every configured rendition exists as real JPEG within its bounds
        versions = _versions(settings, aid)
        expected = {"thumbnail": "WEBP", "preview": "WEBP", "web": "JPEG"}  # previews WebP, download JPEG
        assert {n: s.format.upper() for n, s in RENDITION_SPECS.items()} == expected
        for name, spec in RENDITION_SPECS.items():
            path, params = versions[name]
            raw = _get(settings, path)
            with Image.open(io.BytesIO(raw)) as r:
                assert r.format == expected[name], name
                assert max(r.size) == min(spec.max_px, 2400), (name, r.size)
                assert not r.getexif(), name  # metadata (GPS!) stripped from derived files
            assert params["format"] == spec.format and params["width"] and params["height"]

        # renditions are listed to the client, with their own pixel sizes
        det = (await client.call_tool("get_asset_details", {"asset_id": aid})).structured_content
        listed = {r["name"]: r for r in det["renditions"]}
        assert set(listed) == set(RENDITION_SPECS)
        assert listed["preview"]["mime_type"] == "image/webp"
        assert listed["web"]["mime_type"] == "image/jpeg"

        # HEIC is searchable like everything else
        hits = (await client.call_tool("search_assets", {"query": "Grossglockner"})).structured_content["hits"]
        assert hits[0]["asset_id"] == aid


async def test_renditions_respect_exif_orientation(settings, tenant):  # noqa: F811
    # stored landscape, EXIF says "rotate 90° CW" (6): a portrait photo from a phone
    data = _heic(size=(1600, 1200), rotate_tag=6)
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(client, "hochkant.heic", data)
    aid = res.structured_content["asset_id"]
    path, params = _versions(settings, aid)["preview"]
    with Image.open(io.BytesIO(_get(settings, path))) as r:
        assert r.height > r.width, r.size  # upright
    assert params["height"] > params["width"]
    # file_info reports the displayed (upright) resolution
    info = res.structured_content["file_info"]
    assert (info["width"], info["height"]) == (1200, 1600)


async def test_transparency_kept_in_webp_preview_flattened_in_jpeg_download(settings, tenant):  # noqa: F811
    img = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(client, "transparent.png", buf.getvalue())
    versions = _versions(settings, res.structured_content["asset_id"])
    with Image.open(io.BytesIO(_get(settings, versions["preview"][0]))) as r:
        assert r.format == "WEBP" and r.mode == "RGBA"
        assert r.getpixel((10, 10))[3] == 0
    with Image.open(io.BytesIO(_get(settings, versions["web"][0]))) as r:
        assert r.format == "JPEG"
        assert r.getpixel((10, 10)) == (255, 255, 255)


async def test_delete_removes_all_renditions(settings, tenant):  # noqa: F811
    data = _heic(size=(900, 600))
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "weg.heic", data)).structured_content["asset_id"]
        keys = [p for p, _ in _versions(settings, aid).values()]
        prev = (await client.call_tool("delete_assets", {"asset_ids": [aid]})).structured_content
        await client.call_tool("delete_assets", {"asset_ids": [aid], "confirmation_token": prev["confirmation_token"]})
    s3 = s3_client(settings)
    for k in keys:
        try:
            s3.head_object(Bucket=settings.s3_bucket, Key=k)
            raise AssertionError(f"still present: {k}")
        except s3.exceptions.ClientError:
            pass
