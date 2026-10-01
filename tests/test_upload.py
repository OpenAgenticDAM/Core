"""Chat upload via MCP Apps: a UI-bound tool plus app-only chunked upload tools.

Runs against the real stack (Postgres, SeaweedFS, Ollama) like test_e2e.py.
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
from openagenticdam.server import UPLOAD_CHUNK_BYTES, UPLOAD_UI_URI, build_server
from openagenticdam.storage import s3_client

APP_MIME = "text/html;profile=mcp-app"


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
def tenant(settings):
    t = uuid.uuid4()
    yield t
    with connect(settings) as conn:
        keys = [
            r[0]
            for r in conn.execute(
                "SELECT v.storage_path FROM asset_versions v JOIN assets a ON a.id = v.asset_id WHERE a.tenant_id = %s"
                " UNION SELECT external_id FROM assets WHERE tenant_id = %s",
                (t, t),
            ).fetchall()
        ]
        conn.execute("DELETE FROM audit_log WHERE tenant_id = %s", (t,))
        conn.execute("DELETE FROM assets WHERE tenant_id = %s", (t,))
        conn.execute("DELETE FROM sources WHERE tenant_id = %s", (t,))
        conn.commit()
    s3 = s3_client(settings)
    for k in keys:
        s3.delete_object(Bucket=settings.s3_bucket, Key=k)


def _server(settings, tenant):
    # No explicit principals: uploads use the dev identity (OAD_DEV_PRINCIPALS) like Claude Desktop.
    return build_server(settings, tenant_id=tenant)


def _jpeg(size=(1400, 1000)) -> bytes:
    img = Image.effect_noise(size, 80).convert("RGB")  # noise defeats compression -> multi-chunk file
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


async def _upload(client, name: str, data: bytes, **commit_args):
    begin = await client.call_tool("upload_begin", {"file_name": name, "size_bytes": len(data)})
    assert not begin.is_error, begin.content
    upload_id = begin.structured_content["upload_id"]
    for off in range(0, len(data), UPLOAD_CHUNK_BYTES):
        part = data[off : off + UPLOAD_CHUNK_BYTES]
        res = await client.call_tool(
            "upload_chunk",
            {"upload_id": upload_id, "offset": off, "data_b64": base64.b64encode(part).decode()},
        )
        assert not res.is_error, res.content
    return await client.call_tool("upload_commit", {"upload_id": upload_id, **commit_args})


async def test_upload_assets_tool_is_bound_to_html_ui(settings, tenant):
    async with Client(_server(settings, tenant)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["upload_assets"].meta["ui"]["resourceUri"] == UPLOAD_UI_URI
        res = await client.read_resource(UPLOAD_UI_URI)
        content = res.contents[0]
        assert content.mime_type == APP_MIME
        assert content.text.lstrip().lower().startswith("<!doctype html>")


async def test_chunk_tools_are_hidden_from_the_model(settings, tenant):
    async with Client(_server(settings, tenant)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("upload_begin", "upload_chunk", "upload_commit"):
            assert tools[name].meta["ui"]["visibility"] == ["app"], name


async def test_chunked_upload_creates_searchable_asset(settings, tenant):
    data = _jpeg()
    assert len(data) > 2 * UPLOAD_CHUNK_BYTES  # really exercises multiple chunks
    async with Client(_server(settings, tenant)) as client:
        res = await _upload(client, "IMG_0815.jpg", data, title="Gelbe Ducati am Stilfser Joch", tags=["ducati"])
        assert not res.is_error, res.content
        out = res.structured_content
        assert out["file_name"] == "IMG_0815.jpg"
        assert out["file_size_bytes"] == len(data)
        asset_id = out["asset_id"]

        found = await client.call_tool("search_assets", {"query": "Ducati Stilfser Joch", "limit": 3})
        assert found.structured_content["hits"][0]["asset_id"] == asset_id

    with connect(settings) as conn:
        key = conn.execute("SELECT external_id FROM assets WHERE id = %s", (asset_id,)).fetchone()[0]
        audit = conn.execute(
            "SELECT outcome FROM audit_log WHERE tenant_id = %s AND tool = 'upload_commit'", (tenant,)
        ).fetchall()
    assert key.startswith("chat-uploads/")
    stored = s3_client(settings).get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
    assert stored == data
    assert audit == [("ok",)]


async def test_upload_rejects_file_that_is_not_an_image(settings, tenant):
    fake = b"%PDF-1.7\n" + b"x" * 2000
    async with Client(_server(settings, tenant)) as client:
        res = await _upload(client, "harmlos.jpg", fake)
    assert res.is_error
    assert "invalid_image" in res.content[0].text
    with connect(settings) as conn:
        assert conn.execute("SELECT count(*) FROM assets WHERE tenant_id = %s", (tenant,)).fetchone() == (0,)


async def test_upload_rejects_declared_size_above_limit(settings, tenant):
    async with Client(_server(settings, tenant)) as client:
        res = await client.call_tool(
            "upload_begin", {"file_name": "huge.jpg", "size_bytes": settings.upload_max_bytes + 1}
        )
    assert res.is_error
    assert "too_large" in res.content[0].text


async def test_upload_rejects_out_of_order_chunk(settings, tenant):
    async with Client(_server(settings, tenant)) as client:
        begin = await client.call_tool("upload_begin", {"file_name": "a.jpg", "size_bytes": 10})
        res = await client.call_tool(
            "upload_chunk",
            {"upload_id": begin.structured_content["upload_id"], "offset": 5, "data_b64": "AAAA"},
        )
    assert res.is_error
    assert "bad_offset" in res.content[0].text


async def test_upload_rejects_unknown_or_forged_upload_id(settings, tenant):
    async with Client(_server(settings, tenant)) as client:
        for forged in (str(uuid.uuid4()), "../../etc/passwd"):
            res = await client.call_tool("upload_commit", {"upload_id": forged})
            assert res.is_error
            assert "unknown_upload" in res.content[0].text
