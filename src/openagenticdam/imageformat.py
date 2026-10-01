"""Image format detection from the bytes - names and Content-Types are not trusted.

HEIF is a container; the `ftyp` brand tells what is inside. iPhones write brand `heic`
(HEVC-coded stills) - shown as "HEIC" / image/heic, the name users know. Other HEIF files
(AVC- or generic `mif1`) stay "HEIF" / image/heif.
"""

from __future__ import annotations

import io

import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()

# Pillow format name -> MIME type, for every format the DAM accepts as an image.
FORMAT_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "TIFF": "image/tiff",
    "GIF": "image/gif",
    "HEIF": "image/heif",
}
# Shown to users in upload panels and tool descriptions.
DISPLAY_FORMATS = ["JPEG", "HEIC", "PNG", "WebP", "TIFF", "GIF"]

# ISO/IEC 23008-12 brands of HEVC-coded images and sequences.
HEIC_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"})


def heif_mime(header: bytes) -> str:
    """image/heic for HEVC brands, image/heif for any other HEIF brand."""
    return "image/heic" if header[8:12] in HEIC_BRANDS else "image/heif"


def sniff(data: bytes) -> tuple[str, str] | None:
    """(display format, MIME type) if the bytes are an accepted image, else None.

    Only the header is parsed; pixels are not decoded here.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return None
    if fmt not in FORMAT_MIME:
        return None
    if fmt == "HEIF":
        mime = heif_mime(data[:16])
        return ("HEIC" if mime == "image/heic" else "HEIF"), mime
    return fmt, FORMAT_MIME[fmt]
