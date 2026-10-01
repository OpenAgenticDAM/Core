"""delete_assets: two-step (preview -> confirm), write permission, storage cleanup, audit."""

from __future__ import annotations

import io
import uuid

from mcp import Client
from PIL import Image

from openagenticdam.db import connect
from openagenticdam.ingest import ensure_source, ingest_s3_object
from openagenticdam.server import build_server
from openagenticdam.storage import s3_client
from tests.test_upload import _upload, settings, tenant  # noqa: F401 - shared fixtures


def _jpeg(colour=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(buf, "JPEG")
    return buf.getvalue()


def _exists(settings, key: str) -> bool:  # noqa: F811
    try:
        s3_client(settings).head_object(Bucket=settings.s3_bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 - botocore ClientError 404
        return False


def _row(settings, asset_id: str):  # noqa: F811
    with connect(settings) as conn:
        return conn.execute(
            "SELECT a.external_id, v.storage_path FROM assets a"
            " LEFT JOIN asset_versions v ON v.asset_id = a.id AND v.kind = 'thumbnail' WHERE a.id = %s",
            (asset_id,),
        ).fetchone()


async def test_preview_deletes_nothing_and_returns_token(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        up = (await _upload(client, "keep.jpg", _jpeg())).structured_content
        res = await client.call_tool("delete_assets", {"asset_ids": [up["asset_id"]]})
    assert not res.is_error, res.content
    out = res.structured_content
    assert out["dry_run"] is True
    assert out["deleted"] == []
    assert [a["asset_id"] for a in out["would_delete"]] == [up["asset_id"]]
    assert out["would_delete"][0]["file_name"] == "keep.jpg"
    assert out["would_delete"][0]["original_removed"] is True  # chat upload: we own the original
    assert out["confirmation_token"]
    assert _row(settings, up["asset_id"]) is not None


async def test_confirmed_delete_removes_db_rows_thumbnail_and_owned_original(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        up = (await _upload(client, "weg.jpg", _jpeg(), title="Loeschtest")).structured_content
        key, thumb = _row(settings, up["asset_id"])
        assert _exists(settings, key) and _exists(settings, thumb)

        preview = (await client.call_tool("delete_assets", {"asset_ids": [up["asset_id"]]})).structured_content
        res = await client.call_tool(
            "delete_assets",
            {"asset_ids": [up["asset_id"]], "confirmation_token": preview["confirmation_token"]},
        )
        assert not res.is_error, res.content
        assert res.structured_content["dry_run"] is False
        assert res.structured_content["deleted"] == [up["asset_id"]]

        found = await client.call_tool("search_assets", {"query": "Loeschtest"})
        assert up["asset_id"] not in [h["asset_id"] for h in found.structured_content["hits"]]
        det = await client.call_tool("get_asset_details", {"asset_id": up["asset_id"]})
        assert det.is_error

    assert _row(settings, up["asset_id"]) is None
    assert not _exists(settings, key)
    assert not _exists(settings, thumb)
    with connect(settings) as conn:
        for table in ("embeddings", "asset_acl", "asset_versions"):
            n = conn.execute(f"SELECT count(*) FROM {table} WHERE asset_id = %s", (up["asset_id"],)).fetchone()  # noqa: S608
            assert n == (0,), table
        audit = conn.execute(
            "SELECT outcome, asset_ids FROM audit_log WHERE tenant_id = %s AND tool = 'delete_assets' ORDER BY id",
            (tenant,),
        ).fetchall()
    assert [a[0] for a in audit] == ["preview", "deleted"]
    assert [str(x) for x in audit[1][1]] == [up["asset_id"]]


async def test_token_is_bound_to_exact_asset_set(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        a = (await _upload(client, "a.jpg", _jpeg())).structured_content["asset_id"]
        b = (await _upload(client, "b.jpg", _jpeg((200, 10, 10)))).structured_content["asset_id"]  # distinct bytes
        token = (await client.call_tool("delete_assets", {"asset_ids": [a]})).structured_content["confirmation_token"]
        res = await client.call_tool("delete_assets", {"asset_ids": [a, b], "confirmation_token": token})
    assert res.is_error
    assert "invalid_confirmation" in res.content[0].text
    assert _row(settings, a) is not None and _row(settings, b) is not None


async def test_forged_or_foreign_token_is_rejected(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        a = (await _upload(client, "a.jpg", _jpeg())).structured_content["asset_id"]
        token = (await client.call_tool("delete_assets", {"asset_ids": [a]})).structured_content["confirmation_token"]
    # same asset, other actor -> token must not transfer
    async with Client(build_server(settings, tenant_id=tenant, actor="mallory@localhost")) as other:
        res = await other.call_tool("delete_assets", {"asset_ids": [a], "confirmation_token": token})
        assert res.is_error and "invalid_confirmation" in res.content[0].text
        res = await other.call_tool("delete_assets", {"asset_ids": [a], "confirmation_token": "x" * 40})
        assert res.is_error
    assert _row(settings, a) is not None


async def test_read_only_principal_cannot_delete_and_learns_nothing(settings, tenant):  # noqa: F811
    t = tenant
    key = f"test/{uuid.uuid4()}.jpg"
    s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=_jpeg(), ContentType="image/jpeg")
    with connect(settings) as conn:
        src = ensure_source(conn, t, kind="s3", name="ro", bucket=settings.s3_bucket)
        ro = ingest_s3_object(conn, settings, t, src, key, acl=["group:everyone"])  # read only
        conn.commit()
    try:
        async with Client(build_server(settings, tenant_id=t)) as client:
            res_ro = await client.call_tool("delete_assets", {"asset_ids": [str(ro)]})
            res_missing = await client.call_tool("delete_assets", {"asset_ids": [str(uuid.uuid4())]})
        assert res_ro.is_error and res_missing.is_error
        assert res_ro.content[0].text == res_missing.content[0].text  # no existence oracle
        assert "asset_not_found" in res_ro.content[0].text
        assert _row(settings, str(ro)) is not None
    finally:
        s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=key)


async def test_s3_source_original_is_kept(settings, tenant):  # noqa: F811
    t = tenant
    key = f"test/{uuid.uuid4()}.jpg"
    s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=_jpeg(), ContentType="image/jpeg")
    with connect(settings) as conn:
        src = ensure_source(conn, t, kind="s3", name="rw", bucket=settings.s3_bucket)
        aid = ingest_s3_object(conn, settings, t, src, key, acl=["group:everyone"], write_acl=["group:everyone"])
        conn.commit()
    try:
        async with Client(build_server(settings, tenant_id=t)) as client:
            prev = (await client.call_tool("delete_assets", {"asset_ids": [str(aid)]})).structured_content
            assert prev["would_delete"][0]["original_removed"] is False
            res = await client.call_tool(
                "delete_assets", {"asset_ids": [str(aid)], "confirmation_token": prev["confirmation_token"]}
            )
        assert res.structured_content["deleted"] == [str(aid)]
        assert _row(settings, str(aid)) is None
        assert _exists(settings, key)  # PRD: no write-back into source systems
    finally:
        s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=key)


async def test_delete_tool_is_marked_destructive(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "delete_assets")
    assert tool.annotations is not None
    assert tool.annotations.destructive_hint is True
    assert tool.annotations.read_only_hint is False


async def _seed(client, n: int) -> list[str]:
    return [
        (await _upload(client, f"img{i}.jpg", _jpeg((i * 37 % 256, i * 11 % 256, 99)))).structured_content["asset_id"]
        for i in range(n)
    ]


async def test_delete_all_preview_counts_everything_and_deletes_nothing(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        ids = await _seed(client, 25)
        res = await client.call_tool("delete_assets", {"all_assets": True})
    assert not res.is_error, res.content
    out = res.structured_content
    assert out["dry_run"] is True and out["total"] == 25
    assert len(out["would_delete"]) == 20  # sample only, the count is complete
    assert out["confirmation_token"]
    with connect(settings) as conn:
        assert conn.execute("SELECT count(*) FROM assets WHERE tenant_id = %s", (tenant,)).fetchone() == (len(ids),)


async def test_delete_all_confirmed_removes_every_deletable_asset(settings, tenant):  # noqa: F811
    t = tenant
    key = f"test/{uuid.uuid4()}.jpg"
    s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=_jpeg((1, 1, 1)), ContentType="image/jpeg")
    with connect(settings) as conn:
        src = ensure_source(conn, t, kind="s3", name="ro", bucket=settings.s3_bucket)
        ro = ingest_s3_object(conn, settings, t, src, key, acl=["group:everyone"])  # visible, read only
        conn.commit()
    try:
        async with Client(build_server(settings, tenant_id=t)) as client:
            ids = await _seed(client, 3)
            keys = [_row(settings, i) for i in ids]
            prev = (await client.call_tool("delete_assets", {"all_assets": True})).structured_content
            assert prev["total"] == 3
            assert prev["not_deletable"] == 1  # the read-only one, counted but not touched
            res = await client.call_tool(
                "delete_assets", {"all_assets": True, "confirmation_token": prev["confirmation_token"]}
            )
        assert not res.is_error, res.content
        assert sorted(res.structured_content["deleted"]) == sorted(ids)
        assert all(_row(settings, i) is None for i in ids)
        for original, thumb in keys:
            assert not _exists(settings, original) and not _exists(settings, thumb)
        assert _row(settings, str(ro)) is not None
    finally:
        s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=key)


async def test_delete_all_token_is_void_when_assets_changed_in_between(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        await _seed(client, 2)
        prev = (await client.call_tool("delete_assets", {"all_assets": True})).structured_content
        late = (await _upload(client, "late.jpg", _jpeg((7, 7, 7)))).structured_content["asset_id"]
        res = await client.call_tool(
            "delete_assets", {"all_assets": True, "confirmation_token": prev["confirmation_token"]}
        )
    assert res.is_error and "invalid_confirmation" in res.content[0].text
    assert _row(settings, late) is not None  # nothing deleted, not even the previewed ones
    with connect(settings) as conn:
        assert conn.execute("SELECT count(*) FROM assets WHERE tenant_id = %s", (tenant,)).fetchone() == (3,)


async def test_selected_token_cannot_be_used_for_delete_all(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        a, b = await _seed(client, 2)
        tok = (await client.call_tool("delete_assets", {"asset_ids": [a]})).structured_content["confirmation_token"]
        res = await client.call_tool("delete_assets", {"all_assets": True, "confirmation_token": tok})
    assert res.is_error
    assert _row(settings, a) is not None and _row(settings, b) is not None


async def test_delete_needs_exactly_one_of_ids_or_all(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        (a,) = await _seed(client, 1)
        neither = await client.call_tool("delete_assets", {})
        both = await client.call_tool("delete_assets", {"asset_ids": [a], "all_assets": True})
    assert neither.is_error and both.is_error


async def test_delete_all_with_nothing_deletable(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await client.call_tool("delete_assets", {"all_assets": True})
    assert not res.is_error
    assert res.structured_content["total"] == 0 and res.structured_content["confirmation_token"] is None
