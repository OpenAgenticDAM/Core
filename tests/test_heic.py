"""HEIC/HEIF is a first-class format on every path into the DAM, not only the chat upload.

Object stores rarely carry a usable Content-Type for iPhone files (Finder, `aws s3 cp` and many
sync tools store binary/octet-stream), so the format must come from the bytes.
"""

from __future__ import annotations

import io
import uuid

import pillow_heif
import pytest
from mcp import Client
from PIL import Image

from openagenticdam.db import connect
from openagenticdam.imageformat import heif_mime, sniff
from openagenticdam.ingest import ensure_source, ingest_s3_object
from openagenticdam.server import build_server
from openagenticdam.storage import s3_client
from tests.test_upload import _upload, settings, tenant  # noqa: F401 - shared fixtures

pillow_heif.register_heif_opener()


def _heic(size=(640, 480)) -> bytes:
    buf = io.BytesIO()
    Image.effect_noise(size, 50).convert("RGB").save(buf, format="HEIF", quality=70)
    return buf.getvalue()


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (1, 2, 3)).save(buf, "JPEG")
    return buf.getvalue()


def _ingest_object(settings, tenant, key: str, body: bytes, content_type: str | None):  # noqa: F811
    extra = {"ContentType": content_type} if content_type else {}
    s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=body, **extra)
    with connect(settings) as conn:
        src = ensure_source(conn, tenant, kind="s3", name="sync", bucket=settings.s3_bucket)
        aid = ingest_s3_object(conn, settings, tenant, src, key, acl=["group:everyone"])
        conn.commit()
        row = conn.execute(
            "SELECT mime_type, technical_metadata->>'format', (technical_metadata->>'width')::int,"
            " (SELECT count(*) FROM asset_versions v WHERE v.asset_id = a.id) FROM assets a WHERE id = %s",
            (aid,),
        ).fetchone()
    return aid, row


@pytest.fixture()
def keys(settings):  # noqa: F811
    made: list[str] = []
    yield made
    for k in made:
        s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=k)


@pytest.mark.parametrize("content_type", [None, "binary/octet-stream", "application/octet-stream"])
def test_s3_heic_without_usable_content_type_is_a_full_image_asset(settings, tenant, keys, content_type):  # noqa: F811
    key = f"test/{uuid.uuid4()}/IMG_1234.HEIC"
    keys.append(key)
    _, (mime, fmt, width, renditions) = _ingest_object(settings, tenant, key, _heic(), content_type)
    assert mime == "image/heic"
    assert fmt == "HEIC"
    assert width == 640
    assert renditions == 3


def test_s3_heic_without_extension_is_detected_by_content(settings, tenant, keys):  # noqa: F811
    key = f"test/{uuid.uuid4()}/photo"
    keys.append(key)
    _, (mime, fmt, _, renditions) = _ingest_object(settings, tenant, key, _heic(), None)
    assert (mime, fmt, renditions) == ("image/heic", "HEIC", 3)


def test_wrong_content_type_does_not_override_the_bytes(settings, tenant, keys):  # noqa: F811
    key = f"test/{uuid.uuid4()}/bild.jpg"
    keys.append(key)
    _, (mime, fmt, _, renditions) = _ingest_object(settings, tenant, key, _jpeg(), "text/plain")
    assert (mime, fmt, renditions) == ("image/jpeg", "JPEG", 3)


def test_object_claiming_to_be_an_image_but_is_not_is_indexed_without_renditions(settings, tenant, keys):  # noqa: F811
    key = f"test/{uuid.uuid4()}/fake.heic"
    keys.append(key)
    _, (mime, fmt, width, renditions) = _ingest_object(settings, tenant, key, b"not an image at all", "image/heic")
    assert not mime.startswith("image/")
    assert (fmt, width, renditions) == (None, None, 0)


async def test_chat_upload_reports_heic_not_heif(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(client, "IMG_0001.HEIC", _heic())
        assert not res.is_error, res.content
        info = res.structured_content["file_info"]
        assert (info["format"], info["mime_type"]) == ("HEIC", "image/heic")
        hits = (await client.call_tool("search_assets", {"query": "IMG 0001"})).structured_content["hits"]
    assert hits[0]["file_info"]["format"] == "HEIC"


async def test_upload_tool_advertises_heic(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        limits = (await client.call_tool("upload_assets", {})).structured_content
    assert "HEIC" in limits["formats"]


def test_sniff_formats():
    assert sniff(_jpeg()) == ("JPEG", "image/jpeg")
    assert sniff(_heic()) == ("HEIC", "image/heic")
    assert sniff(b"nope") is None
    assert sniff(b"") is None


@pytest.mark.parametrize(
    ("brand", "expected"),
    [
        (b"heic", "image/heic"),
        (b"heix", "image/heic"),
        (b"hevc", "image/heic"),
        (b"mif1", "image/heif"),
        (b"msf1", "image/heif"),
    ],
)
def test_heif_brand_decides_heic_vs_heif(brand, expected):
    header = b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00mif1"
    assert heif_mime(header) == expected
