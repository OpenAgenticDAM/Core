"""Full metadata extraction with ExifTool, split into general and personal data.

ExifTool reads every metadata family (EXIF, XMP, IPTC, ICC, QuickTime/HEIF boxes, maker notes
of Apple, Canon, Sony, ...). It runs as a separate process on untrusted input: no shell, user
config disabled, bytes via stdin, hard timeout, output size capped per value.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
MAX_STRING = 2000
MAX_LIST = 200

# Numeric output for these tags (decimal degrees / metres) instead of "46 deg 31' 43.00\" N".
NUMERIC_TAGS = ["-GPSLatitude#", "-GPSLongitude#", "-GPSAltitude#", "-GPSImgDirection#"]

_SENSITIVE_TAG = re.compile(
    r"GPS|SerialNumber|OwnerName|PersonInImage|^Region|Face|PhotoIdentifier|ImageUniqueID", re.IGNORECASE
)
_SENSITIVE_GROUP = re.compile(r"^(GPS|XMP-mwg-rs|XMP-MP|XMP-MP1)$")

AI_SOURCE_TYPES = {"trainedAlgorithmicMedia", "compositeWithTrainedAlgorithmicMedia", "algorithmicMedia"}


class MetadataError(RuntimeError):
    """ExifTool missing, crashed, timed out or returned garbage."""


@dataclass
class Extracted:
    tool: str
    general: dict[str, Any]
    sensitive: dict[str, Any]
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    direction_deg: float | None = None
    accuracy_m: float | None = None
    face_count: int = 0
    capture: dict[str, Any] = field(default_factory=dict)
    privacy: dict[str, Any] = field(default_factory=dict)
    captured_at: str | None = None
    search_text: str = ""


@cache
def _version(binary: str) -> str:
    out = subprocess.run([binary, "-ver"], capture_output=True, text=True, timeout=10, check=True)  # noqa: S603
    return out.stdout.strip()


def _resolve(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise MetadataError(f"exiftool not found ({binary!r}); install it or set OAD_EXIFTOOL")
    return path


def is_sensitive(key: str) -> bool:
    group, _, tag = key.partition(":")
    return bool(_SENSITIVE_GROUP.match(group) or _SENSITIVE_TAG.search(tag))


def _cap(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
    if isinstance(value, list):
        return [_cap(v) for v in value[:MAX_LIST]]
    if isinstance(value, dict):
        return {k: _cap(v) for k, v in list(value.items())[:MAX_LIST]}
    return value


def _float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        m = re.match(r"\s*(-?\d+(?:\.\d+)?)", value)
        return float(m.group(1)) if m else None
    return None


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if d.get(k) not in (None, "", [], {}):
            return d[k]
    return None


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def parse_exif_datetime(raw: str) -> str | None:
    """'2026:10:01 20:10:35.970+02:00' -> ISO 8601 (zone and fraction kept when present)."""
    m = re.match(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$", raw.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:  # "0000:00:00 00:00:00" and friends
        return None
    iso = dt.isoformat()
    if frac:
        iso += frac[:4]
    if tz:
        iso += "+00:00" if tz == "Z" else tz
    return iso


def _face_count(data: dict[str, Any]) -> int:
    n = 0
    for key in ("XMP-mwg-rs:RegionInfo", "XMP-MP:RegionInfoMP"):
        info = data.get(key)
        if isinstance(info, dict):
            regions = info.get("RegionList") or info.get("Regions") or []
            n += sum(1 for r in regions if isinstance(r, dict) and str(r.get("Type", "Face")).lower() == "face")
    return n


def _summary(d: dict[str, Any]) -> dict[str, Any]:
    make, model = d.get("IFD0:Make"), d.get("IFD0:Model")
    camera = None
    if model:
        camera = str(model) if make and str(model).startswith(str(make)) else " ".join(filter(None, [make, model]))
    exposure = d.get("ExifIFD:ExposureTime")
    fnum = d.get("ExifIFD:FNumber")
    summary = {
        "camera": camera,
        "lens": _first(d, "ExifIFD:LensModel", "Composite:LensID", "XMP-aux:Lens"),
        "focal_length": _first(d, "Composite:FocalLength35efl", "ExifIFD:FocalLength"),
        "aperture": f"f/{fnum}" if fnum else None,
        "exposure_time": f"{exposure} s" if exposure else None,
        "iso": d.get("ExifIFD:ISO"),
        "flash": d.get("ExifIFD:Flash"),
        "white_balance": d.get("ExifIFD:WhiteBalance"),
        "exposure_program": d.get("ExifIFD:ExposureProgram"),
        "software": _first(d, "IFD0:Software", "XMP-xmp:CreatorTool"),
        "color_profile": d.get("ICC_Profile:ProfileDescription"),
        "bit_depth": _first(d, "QuickTime:ImagePixelDepth", "File:BitsPerSample"),
        "live_photo": "Apple:LivePhotoVideoIndex" in d or None,
        "hdr_gain_map": any(k.startswith("XMP-HDRGainMap:") for k in d)
        or "hdrgainmap" in str(d.get("QuickTime:AuxiliaryImageType", "")).lower()
        or None,
        "copyright": _first(d, "XMP-dc:Rights", "IFD0:Copyright", "IPTC:CopyrightNotice"),
        "creator": _first(d, "XMP-dc:Creator", "IFD0:Artist", "IPTC:By-line"),
        "keywords": _as_list(_first(d, "XMP-dc:Subject", "IPTC:Keywords")) or None,
        "caption": _first(d, "XMP-dc:Description", "IPTC:Caption-Abstract", "IFD0:ImageDescription"),
        "rating": d.get("XMP-xmp:Rating"),
    }
    return {k: v for k, v in summary.items() if v not in (None, "", [], False)}


def extract(data: bytes, binary: str = "exiftool") -> Extracted:
    exe = _resolve(binary)
    cmd = [
        exe,
        "-config",
        "",  # ignore any ~/.ExifTool_config
        "-j",
        "-G1",
        "-a",
        "-struct",
        "-charset",
        "utf8",
        "-api",
        "LargeFileSupport=1",
        "--System:all",
        "--ExifTool:all",
        *NUMERIC_TAGS,
        "-all",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)  # noqa: S603
    except subprocess.TimeoutExpired as exc:
        raise MetadataError("exiftool timed out") from exc
    if proc.returncode not in (0, 1):  # 1 = minor warnings (e.g. unknown trailer)
        raise MetadataError(f"exiftool failed: {proc.stderr.decode(errors='replace')[:200]}")
    try:
        raw: dict[str, Any] = json.loads(proc.stdout.decode("utf-8", errors="replace"))[0]
    except (ValueError, IndexError) as exc:
        raise MetadataError("exiftool returned no readable JSON") from exc
    raw.pop("SourceFile", None)

    general: dict[str, Any] = {}
    sensitive: dict[str, Any] = {}
    for key, value in raw.items():
        (sensitive if is_sensitive(key) else general)[key] = _cap(value)

    ex = Extracted(tool=f"exiftool {_version(exe)}", general=general, sensitive=sensitive)
    ex.latitude = _float(_first(sensitive, "Composite:GPSLatitude", "GPS:GPSLatitude", "XMP-exif:GPSLatitude"))
    ex.longitude = _float(_first(sensitive, "Composite:GPSLongitude", "GPS:GPSLongitude", "XMP-exif:GPSLongitude"))
    if ex.latitude is not None and not -90 <= ex.latitude <= 90:
        ex.latitude = None
    if ex.longitude is not None and not -180 <= ex.longitude <= 180:
        ex.longitude = None
    ex.altitude_m = _float(_first(sensitive, "Composite:GPSAltitude", "GPS:GPSAltitude"))
    ex.direction_deg = _float(sensitive.get("GPS:GPSImgDirection"))
    ex.accuracy_m = _float(sensitive.get("GPS:GPSHPositioningError"))
    ex.face_count = _face_count(sensitive)
    ex.capture = _summary(general)
    ex.privacy = {
        "location_recorded": ex.latitude is not None and ex.longitude is not None,
        "faces_detected": ex.face_count,
        "serial_numbers_recorded": any("SerialNumber" in k for k in sensitive),
    }
    dt = _first(general, "Composite:SubSecDateTimeOriginal", "ExifIFD:DateTimeOriginal", "XMP-photoshop:DateCreated")
    ex.captured_at = parse_exif_datetime(str(dt)) if dt else None
    ex.search_text = " ".join(
        filter(
            None,
            [
                str(general.get("XMP-dc:Title") or general.get("IPTC:ObjectName") or ""),
                str(ex.capture.get("caption") or ""),
                " ".join(ex.capture.get("keywords") or []),
            ],
        )
    )
    return ex


def ai_source_type(general: dict[str, Any]) -> str | None:
    """IPTC DigitalSourceType code (e.g. 'trainedAlgorithmicMedia') if the file declares one."""
    v = _first(general, "XMP-iptcExt:DigitalSourceType", "XMP-plus:DigitalSourceType")
    return str(v).rstrip("/").rsplit("/", 1)[-1] if v else None
