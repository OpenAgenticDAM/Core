"""Identical files (same SHA-256 of the original bytes) are kept once per tenant, automatically."""

from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
import uuid

import psycopg
import pytest
from mcp import Client
from PIL import Image

from openagenticdam.db import connect
from openagenticdam.dedupe import lock_content, remove_duplicate_uploads
from openagenticdam.ingest import ensure_source, ingest_bytes
from openagenticdam.server import CHAT_UPLOAD_SOURCE, build_server
from openagenticdam.storage import s3_client
from tests.test_upload import _upload, settings, tenant  # noqa: F401 - shared fixtures


def _jpeg(colour=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (120, 80), colour).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _live(settings, tenant, data: bytes) -> list[str]:  # noqa: F811
    with connect(settings) as conn:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT id FROM assets WHERE tenant_id = %s AND hash_sha256 = %s AND deleted_at IS NULL",
                (tenant, _sha(data)),
            ).fetchall()
        ]


def _objects(settings) -> set[str]:  # noqa: F811
    s3 = s3_client(settings)
    return {
        o["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket)
        for o in page.get("Contents", [])
    }


def _outcomes(settings, tenant, tool: str) -> list[str]:  # noqa: F811
    with connect(settings) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT outcome FROM audit_log WHERE tenant_id = %s AND tool = %s ORDER BY id", (tenant, tool)
            ).fetchall()
        ]


async def test_second_upload_of_same_bytes_returns_existing_asset_and_stores_nothing(settings, tenant):  # noqa: F811
    data = _jpeg()
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        first = (await _upload(client, "ducati.jpg", data)).structured_content
        before = _objects(settings)
        second = await _upload(client, "ducati_kopie.jpg", data)  # other name, same bytes
    assert not second.is_error, second.content
    out = second.structured_content
    assert out["asset_id"] == first["asset_id"]
    assert out["file_name"] == "ducati.jpg"
    assert out["duplicate_of"] == first["asset_id"]
    assert "ducati.jpg" in out["message"] and "already" in out["message"]
    assert _live(settings, tenant, data) == [first["asset_id"]]
    assert _objects(settings) == before  # neither original nor renditions written twice
    assert _outcomes(settings, tenant, "upload_commit") == ["ok", "duplicate_skipped"]


async def test_begin_with_known_hash_skips_the_transfer(settings, tenant):  # noqa: F811
    data = _jpeg()
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        first = (await _upload(client, "x.jpg", data)).structured_content["asset_id"]
        res = await client.call_tool(
            "upload_begin", {"file_name": "x.jpg", "size_bytes": len(data), "sha256": _sha(data)}
        )
        assert not res.is_error, res.content
        assert res.structured_content["duplicate_of"] == first
        assert res.structured_content["upload_id"] is None  # nothing to send
        fresh = await client.call_tool("upload_begin", {"file_name": "y.jpg", "size_bytes": 10, "sha256": "0" * 64})
        assert fresh.structured_content["upload_id"] and fresh.structured_content["duplicate_of"] is None


async def test_begin_rejects_malformed_hash(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await client.call_tool("upload_begin", {"file_name": "y.jpg", "size_bytes": 10, "sha256": "zz"})
    assert res.is_error


async def test_client_hash_is_not_trusted_commit_recomputes_it(settings, tenant):  # noqa: F811
    data = _jpeg()
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        first = (await _upload(client, "x.jpg", data)).structured_content["asset_id"]
        begin = await client.call_tool(
            "upload_begin", {"file_name": "x.jpg", "size_bytes": len(data), "sha256": "1" * 64}
        )
        uid = begin.structured_content["upload_id"]
        await client.call_tool(
            "upload_chunk", {"upload_id": uid, "offset": 0, "data_b64": base64.b64encode(data).decode()}
        )
        res = await client.call_tool("upload_commit", {"upload_id": uid})
    assert res.structured_content["duplicate_of"] == first
    assert _live(settings, tenant, data) == [first]


async def test_one_changed_pixel_is_a_different_asset(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        a = (await _upload(client, "a.jpg", _jpeg((200, 30, 30)))).structured_content
        b = (await _upload(client, "b.jpg", _jpeg((200, 30, 31)))).structured_content
    assert a["asset_id"] != b["asset_id"] and b["duplicate_of"] is None


async def test_after_delete_the_same_file_can_be_uploaded_again(settings, tenant):  # noqa: F811
    data = _jpeg()
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "x.jpg", data)).structured_content["asset_id"]
        prev = (await client.call_tool("delete_assets", {"asset_ids": [aid]})).structured_content
        await client.call_tool("delete_assets", {"asset_ids": [aid], "confirmation_token": prev["confirmation_token"]})
        again = (await _upload(client, "x.jpg", data)).structured_content
    assert again["asset_id"] != aid and again["duplicate_of"] is None


async def test_duplicate_of_invisible_asset_reveals_nothing_and_stores_the_upload(settings, tenant):  # noqa: F811
    data = _jpeg((5, 5, 250))
    key = f"test/{uuid.uuid4()}.jpg"
    s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType="image/jpeg")
    with connect(settings) as conn:
        src = ensure_source(conn, tenant, kind="s3", name="secret", bucket=settings.s3_bucket)
        hidden = ingest_bytes(conn, settings, tenant, src, key, data, "image/jpeg", file_name="geheim.jpg", acl=[])
        conn.commit()
    try:
        async with Client(build_server(settings, tenant_id=tenant)) as client:
            begin = await client.call_tool(
                "upload_begin", {"file_name": "m.jpg", "size_bytes": len(data), "sha256": _sha(data)}
            )
            res = await _upload(client, "meins.jpg", data)
        everything = str(res.structured_content) + res.content[0].text + str(begin.structured_content)
        assert str(hidden) not in everything and "geheim" not in everything
        assert begin.structured_content["upload_id"] and begin.structured_content["duplicate_of"] is None
        assert res.structured_content["duplicate_of"] is None
        assert len(_live(settings, tenant, data)) == 2
    finally:
        s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=key)


async def test_other_tenant_may_hold_the_same_file(settings, tenant):  # noqa: F811
    data = _jpeg((1, 2, 3))
    other = uuid.uuid4()
    try:
        async with Client(build_server(settings, tenant_id=tenant)) as client:
            await _upload(client, "x.jpg", data)
        async with Client(build_server(settings, tenant_id=other)) as client:
            res = (await _upload(client, "x.jpg", data)).structured_content
        assert res["duplicate_of"] is None
    finally:
        with connect(settings) as conn:
            keys = [r[0] for r in conn.execute("SELECT external_id FROM assets WHERE tenant_id = %s", (other,))]
            conn.execute("DELETE FROM audit_log WHERE tenant_id = %s", (other,))
            conn.execute("DELETE FROM assets WHERE tenant_id = %s", (other,))
            conn.execute("DELETE FROM sources WHERE tenant_id = %s", (other,))
            conn.commit()
        for k in keys:
            s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=k)


def test_existing_duplicate_uploads_are_removed_oldest_kept(settings, tenant):  # noqa: F811
    data, other = _jpeg((9, 9, 9)), _jpeg((8, 8, 8))
    ids, keys = [], []
    with connect(settings) as conn:
        up = ensure_source(conn, tenant, kind="upload", name=CHAT_UPLOAD_SOURCE, bucket=settings.s3_bucket)
        mirror = ensure_source(conn, tenant, kind="s3", name="mirror", bucket=settings.s3_bucket)
        plan = [(up, data), (up, data), (up, data), (up, other), (mirror, data), (mirror, data)]
        for i, (src, blob) in enumerate(plan):
            key = f"{CHAT_UPLOAD_SOURCE if src == up else 'test'}/{uuid.uuid4()}/f{i}.jpg"
            s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=blob, ContentType="image/jpeg")
            ids.append(
                ingest_bytes(conn, settings, tenant, src, key, blob, "image/jpeg", file_name=f"f{i}.jpg", acl=["a"])
            )
            keys.append(key)
            conn.commit()
            time.sleep(0.01)  # distinct created_at
        removed, objects = remove_duplicate_uploads(conn, tenant)
        conn.commit()
    try:
        assert sorted(removed) == sorted([ids[1], ids[2]])  # oldest upload copy stays
        assert keys[1] in objects and keys[2] in objects  # their originals go too
        assert not {keys[0], keys[3], keys[4], keys[5]} & set(objects)  # kept copy + S3 mirror untouched
        assert set(_live(settings, tenant, data)) == {str(ids[0]), str(ids[4]), str(ids[5])}
    finally:
        for k in keys:
            s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=k)


def test_content_lock_serialises_same_hash_only(settings, tenant):  # noqa: F811
    held, release = threading.Event(), threading.Event()

    def holder():
        with connect(settings) as conn:
            lock_content(conn, tenant, "a" * 64)
            held.set()
            release.wait(5)
            conn.rollback()

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    try:
        with connect(settings) as conn:
            conn.execute("SET lock_timeout = '300ms'")
            lock_content(conn, tenant, "b" * 64)  # other content: not blocked
            conn.rollback()
            conn.execute("SET lock_timeout = '300ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                lock_content(conn, tenant, "a" * 64)  # same content: waits
            conn.rollback()
    finally:
        release.set()
        t.join()


async def test_upload_commit_waits_for_the_content_lock(settings, tenant):  # noqa: F811
    """Proves commit takes the lock: while another transaction holds it, commit cannot finish."""
    data = _jpeg((42, 42, 42))
    held, release = threading.Event(), threading.Event()

    def holder():
        with connect(settings) as conn:
            lock_content(conn, tenant, _sha(data))
            held.set()
            release.wait(5)
            conn.rollback()

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    threading.Timer(1.0, release.set).start()
    started = time.monotonic()
    try:
        async with Client(build_server(settings, tenant_id=tenant)) as client:
            res = await _upload(client, "x.jpg", data)
    finally:
        release.set()
        t.join()
    assert not res.is_error, res.content
    assert time.monotonic() - started >= 0.9
