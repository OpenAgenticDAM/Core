"""Every asset response carries a file-info block: name, format, resolution, size, created, uploaded."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

from mcp import Client
from PIL import Image

from openagenticdam.db import connect
from openagenticdam.server import build_server
from tests.test_upload import _upload, settings, tenant  # noqa: F401 - shared fixtures

EXIF_IFD = 0x8769
DATETIME_ORIGINAL = 0x9003


def _jpeg_with_exif(taken: str | None) -> bytes:
    img = Image.effect_noise((1200, 900), 60).convert("RGB")
    exif = Image.Exif()
    if taken:
        exif.get_ifd(EXIF_IFD)[DATETIME_ORIGINAL] = taken
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, exif=exif)
    return buf.getvalue()


def _assert_info(info: dict, *, name: str, size: int, created: str | None, source: str | None) -> None:
    assert info["file_name"] == name
    assert info["format"] == "JPEG"
    assert info["mime_type"] == "image/jpeg"
    assert (info["width"], info["height"]) == (1200, 900)
    assert info["resolution"] == "1200 × 900 px"
    assert info["megapixels"] == 1.1
    assert info["file_size_bytes"] == size
    assert info["file_size"].endswith(("KB", "MB"))
    assert info["created_at"] == created
    assert info["created_at_source"] == source
    uploaded = datetime.fromisoformat(info["uploaded_at"])
    assert uploaded.tzinfo is not None
    assert abs((datetime.now(UTC) - uploaded).total_seconds()) < 120


async def test_file_info_in_upload_search_and_details_with_exif_capture_time(settings, tenant):  # noqa: F811
    data = _jpeg_with_exif("2026:07:14 10:32:05")
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(
            client,
            "IMG_4711.jpg",
            data,
            title="Stilfser Joch Kehre 48",
            file_modified_at="2026-07-20T18:00:00Z",
        )
        assert not res.is_error, res.content
        expect = {"name": "IMG_4711.jpg", "size": len(data), "created": "2026-07-14T10:32:05", "source": "exif"}
        _assert_info(res.structured_content["file_info"], **expect)
        asset_id = res.structured_content["asset_id"]

        hit = (await client.call_tool("search_assets", {"query": "Stilfser Joch"})).structured_content["hits"][0]
        assert hit["asset_id"] == asset_id
        _assert_info(hit["file_info"], **expect)

        det = await client.call_tool("get_asset_details", {"asset_id": asset_id})
        _assert_info(det.structured_content["file_info"], **expect)
        # the human-readable preview label carries the key facts too
        label = " ".join(c.text for c in det.content if c.type == "text")
        assert "1200 × 900 px" in label


async def test_every_asset_carries_its_uuid_in_file_info_and_label(settings, tenant):  # noqa: F811
    data = _jpeg_with_exif(None)
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        a = (await _upload(client, "a.jpg", data, title="Asset A")).structured_content
        b = (await _upload(client, "b.jpg", data, title="Asset B")).structured_content
        for up in (a, b):
            assert str(uuid.UUID(up["file_info"]["asset_id"])) == up["asset_id"]  # canonical UUID
        assert a["asset_id"] != b["asset_id"]  # identical bytes still get distinct ids

        res = await client.call_tool("search_assets", {"query": "Asset", "limit": 5})
        for hit in res.structured_content["hits"]:
            assert hit["file_info"]["asset_id"] == hit["asset_id"]
        labels = [c.text for c in res.content if c.type == "text"][1:]  # [0] is the JSON payload
        assert any(f"UUID {a['asset_id']}" in t for t in labels)

        det = await client.call_tool("get_asset_details", {"asset_id": b["asset_id"]})
        assert det.structured_content["file_info"]["asset_id"] == b["asset_id"]
        assert f"UUID {b['asset_id']}" in " ".join(c.text for c in det.content if c.type == "text")


async def test_created_at_falls_back_to_file_modification_time(settings, tenant):  # noqa: F811
    data = _jpeg_with_exif(None)
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(client, "scan.jpg", data, file_modified_at="2026-05-01T08:15:00Z")
    _assert_info(
        res.structured_content["file_info"],
        name="scan.jpg",
        size=len(data),
        created="2026-05-01T08:15:00+00:00",
        source="file",
    )


async def test_created_at_is_null_when_unknown(settings, tenant):  # noqa: F811
    data = _jpeg_with_exif(None)
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(client, "unknown.jpg", data)
    info = res.structured_content["file_info"]
    assert info["created_at"] is None
    assert info["created_at_source"] is None


def test_s3_ingest_records_exif_capture_time(settings):  # noqa: F811
    from openagenticdam.ingest import ensure_source, ingest_s3_object
    from openagenticdam.storage import s3_client

    t = uuid.uuid4()
    key = f"test/{uuid.uuid4()}.jpg"
    s3 = s3_client(settings)
    s3.put_object(
        Bucket=settings.s3_bucket, Key=key, Body=_jpeg_with_exif("2025:12:24 17:00:00"), ContentType="image/jpeg"
    )
    try:
        with connect(settings) as conn:
            src = ensure_source(conn, t, kind="s3", name="b", bucket=settings.s3_bucket)
            aid = ingest_s3_object(conn, settings, t, src, key, acl=["group:everyone"])
            tm = conn.execute("SELECT technical_metadata FROM assets WHERE id = %s", (aid,)).fetchone()[0]
            conn.rollback()
    finally:
        s3.delete_object(Bucket=settings.s3_bucket, Key=key)
        for name, ext in (("thumbnail", "webp"), ("preview", "webp"), ("web", "jpg")):
            s3.delete_object(Bucket=settings.s3_bucket, Key=f"renditions/{aid}/{name}.{ext}")
    assert tm["captured_at"] == "2025-12-24T17:00:00"
    assert tm["file_modified_at"]  # S3 LastModified is recorded as fallback
