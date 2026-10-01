"""End-to-end slice through the real stack: S3 object -> ingest -> MCP tools -> audit log.

Needs scripts/infra-up.sh, `alembic upgrade head` and native Ollama with the embed model.
"""

from __future__ import annotations

import base64
import io
import uuid

import httpx
import psycopg
import pytest
from mcp import Client
from PIL import Image

from openagenticdam.config import Settings
from openagenticdam.db import connect
from openagenticdam.ingest import ensure_source, ingest_s3_object
from openagenticdam.server import build_server
from openagenticdam.storage import s3_client


@pytest.fixture(scope="module")
def settings() -> Settings:
    s = Settings()
    try:
        connect(s).close()
        httpx.get(f"{s.ollama_url}/api/version", timeout=2).raise_for_status()
        s3_client(s).head_bucket(Bucket=s.s3_bucket)
    except (psycopg.OperationalError, httpx.HTTPError, Exception) as exc:  # noqa: BLE001 - skip reason only
        pytest.skip(f"local stack not reachable: {exc}")
    return s


@pytest.fixture()
def seeded(settings):
    """Upload one red JPEG to S3, ingest it for a fresh tenant, clean up afterwards."""
    tenant = uuid.uuid4()
    key = f"test/{uuid.uuid4()}.jpg"
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (220, 20, 20)).save(buf, "JPEG")
    s3 = s3_client(settings)
    s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=buf.getvalue(), ContentType="image/jpeg")

    with connect(settings) as conn:
        source_id = ensure_source(conn, tenant, kind="s3", name="test-bucket", bucket=settings.s3_bucket)
        asset_id = ingest_s3_object(
            conn,
            settings,
            tenant,
            source_id,
            key,
            title="Rotes Testbild",
            acl=["group:everyone"],
        )
        conn.commit()

    yield tenant, source_id, asset_id, key

    with connect(settings) as conn:
        conn.execute("DELETE FROM audit_log WHERE tenant_id = %s", (tenant,))
        conn.execute("DELETE FROM assets WHERE tenant_id = %s", (tenant,))
        conn.execute("DELETE FROM sources WHERE tenant_id = %s", (tenant,))
        conn.commit()
    s3.delete_object(Bucket=settings.s3_bucket, Key=key)
    for name, ext in (("thumbnail", "webp"), ("preview", "webp"), ("web", "jpg")):
        s3.delete_object(Bucket=settings.s3_bucket, Key=f"renditions/{asset_id}/{name}.{ext}")


def test_ingest_extracts_hash_size_dimensions_and_thumbnail(settings, seeded):
    tenant, _, asset_id, _ = seeded
    with connect(settings) as conn:
        row = conn.execute(
            "SELECT hash_sha256, file_size_bytes, technical_metadata->>'width', technical_metadata->>'height'"
            " FROM assets WHERE id = %s",
            (asset_id,),
        ).fetchone()
        thumbs = conn.execute(
            "SELECT storage_path FROM asset_versions WHERE asset_id = %s AND kind = 'thumbnail'", (asset_id,)
        ).fetchall()
        emb = conn.execute("SELECT count(*) FROM embeddings WHERE asset_id = %s", (asset_id,)).fetchone()
    assert row is not None and len(row[0]) == 64 and row[1] > 0
    assert (row[2], row[3]) == ("64", "48")
    assert thumbs == [(f"renditions/{asset_id}/thumbnail.webp",)]
    assert emb == (1,)


async def _call(settings, tenant, principals, tool, args):
    server = build_server(settings, tenant_id=tenant, principals=principals)
    async with Client(server) as client:
        return await client.call_tool(tool, args)


async def test_search_assets_tool_finds_ingested_asset(settings, seeded):
    tenant, source_id, asset_id, _ = seeded
    res = await _call(settings, tenant, [f"{source_id}:group:everyone"], "search_assets", {"query": "rotes Bild"})
    assert not res.is_error
    hits = res.structured_content["hits"]
    assert [h["asset_id"] for h in hits] == [str(asset_id)]


async def test_search_assets_returns_thumbnails_as_inline_image_content(settings, seeded):
    # MCP clients (Claude Desktop) do not fetch arbitrary URLs, least of all localhost:
    # thumbnails must travel inside the tool result as image content blocks.
    tenant, source_id, asset_id, _ = seeded
    res = await _call(settings, tenant, [f"{source_id}:group:everyone"], "search_assets", {"query": "rotes Bild"})
    images = [c for c in res.content if c.type == "image"]
    assert len(images) == 1
    assert images[0].mime_type == "image/webp"
    raw = base64.b64decode(images[0].data)
    with Image.open(io.BytesIO(raw)) as img:
        assert img.format == "WEBP"
        assert max(img.size) <= 256
    # every image is labelled so the model can map it to a hit
    texts = " ".join(c.text for c in res.content if c.type == "text")
    assert str(asset_id) in texts


async def test_get_asset_details_returns_preview_image(settings, seeded):
    tenant, source_id, asset_id, _ = seeded
    res = await _call(
        settings, tenant, [f"{source_id}:group:everyone"], "get_asset_details", {"asset_id": str(asset_id)}
    )
    assert [c.type for c in res.content].count("image") == 1


async def test_search_assets_tool_hides_asset_without_rights(settings, seeded):
    tenant, source_id, _, _ = seeded
    res = await _call(settings, tenant, [f"{source_id}:group:marketing"], "search_assets", {"query": "rotes Bild"})
    assert res.structured_content["hits"] == []


async def test_get_asset_details_denied_without_rights_returns_not_found(settings, seeded):
    tenant, source_id, asset_id, _ = seeded
    res = await _call(
        settings, tenant, [f"{source_id}:group:marketing"], "get_asset_details", {"asset_id": str(asset_id)}
    )
    # Same answer as a non-existent asset: no existence oracle.
    assert res.is_error
    assert "asset_not_found" in res.content[0].text


async def test_get_asset_details_marks_ai_text_as_untrusted_data(settings, seeded):
    tenant, source_id, asset_id, _ = seeded
    res = await _call(
        settings, tenant, [f"{source_id}:group:everyone"], "get_asset_details", {"asset_id": str(asset_id)}
    )
    assert not res.is_error
    d = res.structured_content
    assert d["file_name"].endswith(".jpg")
    assert d["technical_metadata"]["width"] == 64
    assert d["untrusted_content_notice"].startswith("Fields under ai_metadata")


async def test_list_sources_tool(settings, seeded):
    tenant, source_id, _, _ = seeded
    res = await _call(settings, tenant, [f"{source_id}:group:everyone"], "list_sources", {})
    assert [s["id"] for s in res.structured_content["sources"]] == [str(source_id)]


async def test_every_tool_call_is_audited(settings, seeded):
    tenant, source_id, asset_id, _ = seeded
    await _call(settings, tenant, [f"{source_id}:group:everyone"], "search_assets", {"query": "rot"})
    await _call(settings, tenant, [f"{source_id}:group:marketing"], "get_asset_details", {"asset_id": str(asset_id)})
    with connect(settings) as conn:
        rows = conn.execute(
            "SELECT tool, outcome, asset_ids FROM audit_log WHERE tenant_id = %s ORDER BY id", (tenant,)
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("search_assets", "ok"), ("get_asset_details", "denied_or_missing")]
    assert rows[0][2] == [asset_id]
