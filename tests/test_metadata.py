"""Full metadata extraction (ExifTool): everything is stored; personal data is kept apart and never returned."""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

import pillow_heif
import pytest
from mcp import Client
from PIL import Image, ImageCms

from openagenticdam.db import connect
from openagenticdam.ingest import ensure_source, ingest_bytes
from openagenticdam.metadata import MetadataError, extract
from openagenticdam.server import build_server
from openagenticdam.storage import s3_client
from tests.test_upload import _upload, settings, tenant  # noqa: F401 - shared fixtures

pillow_heif.register_heif_opener()

EXIF_IFD, GPS_IFD = 0x8769, 0x8825
LAT, LON = 46.528612, 10.453114  # Stilfser Joch - test value only

XMP = """<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"
  xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#"
  xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">
 <dc:subject><rdf:Bag><rdf:li>Stelvio</rdf:li><rdf:li>Kehre48</rdf:li></rdf:Bag></dc:subject>
 <dc:description><rdf:Alt><rdf:li xml:lang="x-default">Serpentinen am Pass</rdf:li></rdf:Alt></dc:description>
 <dc:rights><rdf:Alt><rdf:li xml:lang="x-default">(c) Jane Example</rdf:li></rdf:Alt></dc:rights>
 <Iptc4xmpExt:DigitalSourceType>http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia</Iptc4xmpExt:DigitalSourceType>
 <mwg-rs:Regions rdf:parseType="Resource"><mwg-rs:RegionList><rdf:Bag><rdf:li rdf:parseType="Resource">
  <mwg-rs:Type>Face</mwg-rs:Type><mwg-rs:Name>Max Mustermann</mwg-rs:Name>
  <mwg-rs:Area stArea:x="0.4" stArea:y="0.4" stArea:w="0.1" stArea:h="0.1" stArea:unit="normalized"/>
 </rdf:li></rdf:Bag></mwg-rs:RegionList></mwg-rs:Regions>
</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end='w'?>"""


def _dms(v: float) -> tuple:
    d = int(v)
    m = int((v - d) * 60)
    s = round(((v - d) * 60 - m) * 60, 4)
    return (d, m, s)


def _rich_jpeg(description_len: int = 0) -> bytes:
    img = Image.effect_noise((800, 600), 40).convert("RGB")
    exif = Image.Exif()
    exif[0x010F] = "TestCam"  # Make
    exif[0x0110] = "X1"  # Model
    ifd = exif.get_ifd(EXIF_IFD)
    ifd[0x9003] = "2026:07:14 10:32:05"  # DateTimeOriginal
    ifd[0x9011] = "+02:00"  # OffsetTimeOriginal
    ifd[0x829D] = 2.8  # FNumber
    ifd[0x829A] = 1 / 125  # ExposureTime
    ifd[0x8827] = 400  # ISO
    ifd[0x920A] = 35.0  # FocalLength
    ifd[0xA434] = "TestLens 35mm"  # LensModel
    ifd[0xA431] = "SN-1234567"  # BodySerialNumber (sensitive)
    gps = exif.get_ifd(GPS_IFD)
    gps[1], gps[2] = "N", _dms(LAT)
    gps[3], gps[4] = "E", _dms(LON)
    xmp = XMP
    if description_len:
        xmp = xmp.replace("Serpentinen am Pass", "A" * description_len)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, exif=exif, xmp=xmp.encode())
    return buf.getvalue()


def _meta(settings, asset_id: str):  # noqa: F811
    with connect(settings) as conn:
        normal = conn.execute("SELECT tool, data FROM asset_metadata WHERE asset_id = %s", (asset_id,)).fetchone()
        sens = conn.execute(
            "SELECT latitude, longitude, face_count, data FROM asset_sensitive_metadata WHERE asset_id = %s",
            (asset_id,),
        ).fetchone()
    return normal, sens


async def test_everything_is_stored_personal_data_separately(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        res = await _upload(client, "pass.jpg", _rich_jpeg())
        assert not res.is_error, res.content
        aid = res.structured_content["asset_id"]
    normal, sens = _meta(settings, aid)
    tool, data = normal
    assert tool.startswith("exiftool ")
    assert data["IFD0:Make"] == "TestCam"
    assert data["ExifIFD:ISO"] == 400
    assert data["ExifIFD:LensModel"] == "TestLens 35mm"
    assert data["XMP-dc:Subject"] == ["Stelvio", "Kehre48"]
    assert data["XMP-iptcExt:DigitalSourceType"].endswith("trainedAlgorithmicMedia")
    # really "everything": general + sensitive == every tag ExifTool reports, nothing dropped
    full = extract(_rich_jpeg(), settings.exiftool)
    assert set(data) == set(full.general)
    assert set(data).isdisjoint(sens[3])
    assert len(data) + len(sens[3]) == len(full.general) + len(full.sensitive)
    # nothing personal in the general table
    flat = json.dumps(data)
    assert "GPS" not in flat and "SN-1234567" not in flat and "Max Mustermann" not in flat
    assert "RegionInfo" not in flat
    # ... but stored apart
    lat, lon, faces, sdata = sens
    assert abs(lat - LAT) < 1e-4 and abs(lon - LON) < 1e-4
    assert faces == 1
    assert any("SerialNumber" in k for k in sdata)
    assert any("Region" in k for k in sdata)


async def test_details_show_capture_summary_but_no_personal_data(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "pass.jpg", _rich_jpeg())).structured_content["asset_id"]
        det = await client.call_tool("get_asset_details", {"asset_id": aid})
    d = det.structured_content
    cap = d["capture"]
    assert cap["camera"] == "TestCam X1"
    assert cap["lens"] == "TestLens 35mm"
    assert cap["iso"] == 400
    assert cap["aperture"] == "f/2.8"
    assert cap["exposure_time"] == "1/125 s"
    assert cap["focal_length"].startswith("35")
    assert d["file_info"]["created_at"] == "2026-07-14T10:32:05+02:00"
    assert d["privacy"] == {"location_recorded": True, "faces_detected": 1, "serial_numbers_recorded": True}
    assert d["metadata"]["XMP-dc:Subject"] == ["Stelvio", "Kehre48"]
    assert d["ai_generated_flag"] == "trainedAlgorithmicMedia"
    everything = json.dumps(d) + " ".join(c.text for c in det.content if c.type == "text")
    assert str(LAT)[:6] not in everything and str(LON)[:6] not in everything
    assert "SN-1234567" not in everything and "Max Mustermann" not in everything


async def test_embedded_keywords_and_caption_make_asset_searchable(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "IMG_0001.jpg", _rich_jpeg())).structured_content["asset_id"]
        hits = (await client.call_tool("search_assets", {"query": "Stelvio Serpentinen"})).structured_content
    assert hits["hits"][0]["asset_id"] == aid


async def test_oversized_text_values_are_capped(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "long.jpg", _rich_jpeg(description_len=20_000))).structured_content["asset_id"]
    data = _meta(settings, aid)[0][1]
    assert 0 < len(data["XMP-dc:Description"]) <= 2001


async def test_heic_from_iphone_style_file(settings, tenant):  # noqa: F811
    img = Image.effect_noise((640, 480), 40).convert("RGB")
    exif = Image.Exif()
    exif[0x010F], exif[0x0110] = "Apple", "iPhone 14 Pro"
    exif.get_ifd(EXIF_IFD)[0x9003] = "2026:10:01 20:10:35"
    buf = io.BytesIO()
    img.save(buf, format="HEIF", exif=exif.tobytes())
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "IMG_8419.HEIC", buf.getvalue())).structured_content["asset_id"]
    data = _meta(settings, aid)[0][1]
    assert data["File:FileType"] == "HEIC"
    assert data["IFD0:Model"] == "iPhone 14 Pro"


def test_missing_exiftool_fails_loudly(settings, tenant):  # noqa: F811
    broken = settings.model_copy(update={"exiftool": "/nonexistent/exiftool"})
    key = f"test/{uuid.uuid4()}.jpg"
    with connect(settings) as conn:
        src = ensure_source(conn, tenant, kind="s3", name="x", bucket=settings.s3_bucket)
        with pytest.raises(MetadataError):
            ingest_bytes(conn, broken, tenant, src, key, _rich_jpeg(), "image/jpeg", file_name="x.jpg", acl=[])
        conn.rollback()


async def test_delete_removes_metadata_rows(settings, tenant):  # noqa: F811
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "weg.jpg", _rich_jpeg())).structured_content["asset_id"]
        prev = (await client.call_tool("delete_assets", {"asset_ids": [aid]})).structured_content
        await client.call_tool("delete_assets", {"asset_ids": [aid], "confirmation_token": prev["confirmation_token"]})
    assert _meta(settings, aid) == (None, None)


P3 = Path("/System/Library/ColorSync/Profiles/Display P3.icc")


@pytest.mark.skipif(not P3.exists(), reason="needs macOS Display P3 profile")
async def test_renditions_convert_embedded_icc_profile_to_srgb(settings, tenant):  # noqa: F811
    p3 = P3.read_bytes()
    colour = (60, 150, 230)
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), colour).save(buf, "JPEG", quality=100, icc_profile=p3)
    expected = ImageCms.profileToProfile(
        Image.new("RGB", (1, 1), colour),
        ImageCms.ImageCmsProfile(io.BytesIO(p3)),
        ImageCms.createProfile("sRGB"),
        outputMode="RGB",
    ).getpixel((0, 0))
    assert expected != colour  # P3 and sRGB really differ for this colour
    async with Client(build_server(settings, tenant_id=tenant)) as client:
        aid = (await _upload(client, "p3.jpg", buf.getvalue())).structured_content["asset_id"]
    with connect(settings) as conn:
        path, params = conn.execute(
            "SELECT storage_path, params FROM asset_versions WHERE asset_id = %s AND params->>'rendition' = 'web'",
            (aid,),
        ).fetchone()
    raw = s3_client(settings).get_object(Bucket=settings.s3_bucket, Key=path)["Body"].read()
    with Image.open(io.BytesIO(raw)) as r:
        got = r.convert("RGB").getpixel((200, 150))
    assert all(abs(a - b) <= 4 for a, b in zip(got, expected, strict=True)), (got, expected)
    assert params["color_managed"] is True


def test_unique_device_and_photo_ids_count_as_personal():
    from openagenticdam.metadata import is_sensitive

    for key in (
        "Apple:PhotoIdentifier",  # links to the user's iCloud photo library
        "ExifIFD:ImageUniqueID",
        "ExifIFD:BodySerialNumber",
        "XMP-aux:SerialNumber",
        "Canon:InternalSerialNumber",
        "ExifIFD:OwnerName",
        "XMP-iptcExt:PersonInImage",
        "XMP-mwg-rs:RegionInfo",
        "GPS:GPSLatitude",
        "XMP-exif:GPSLongitude",
    ):
        assert is_sensitive(key), key
    for key in ("IFD0:Make", "ExifIFD:ISO", "XMP-dc:Subject", "Apple:HDRHeadroom", "XMP-xmpMM:DocumentID"):
        assert not is_sensitive(key), key
