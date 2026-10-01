"""Renditions derived from the stored original (any format Pillow can decode, incl. HEIF/HEIC).

The original is never modified. Renditions are deterministic: EXIF orientation applied, colour
converted to RGB, metadata stripped (privacy: no GPS in derived files that may be shared), then
downscaled - never upscaled. Previews are WebP (small, keeps transparency); the download
rendition is JPEG (opens everywhere, transparency flattened on white).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pillow_heif
from PIL import Image, ImageCms, ImageOps

pillow_heif.register_heif_opener()

FORMATS = {"webp": ("WEBP", "image/webp"), "jpeg": ("JPEG", "image/jpeg")}


@dataclass(frozen=True)
class Spec:
    max_px: int
    quality: int
    format: str  # key of FORMATS


RENDITION_SPECS: dict[str, Spec] = {
    "thumbnail": Spec(256, 75, "webp"),
    "preview": Spec(1024, 80, "webp"),
    "web": Spec(2048, 85, "jpeg"),
}


@dataclass(frozen=True)
class Rendition:
    name: str
    spec: Spec
    data: bytes
    width: int
    height: int
    color_managed: bool = False

    @property
    def mime_type(self) -> str:
        return FORMATS[self.spec.format][1]

    @property
    def ext(self) -> str:
        return "jpg" if self.spec.format == "jpeg" else self.spec.format


def upright(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation so width/height match what a viewer displays."""
    return ImageOps.exif_transpose(img)


def has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)


def to_rgb(img: Image.Image) -> Image.Image:
    """Flatten transparency on white; everything else straight to RGB."""
    if has_alpha(img):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    return img.convert("RGB")


_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def to_srgb(img: Image.Image, icc: bytes | None) -> tuple[Image.Image, bool]:
    """Convert from the embedded profile (Display P3 on iPhones, Adobe RGB, ...) to sRGB.

    Renditions carry no profile, and browsers treat untagged images as sRGB - without this
    step wide-gamut colours come out dull. Broken profiles fall back to an untouched copy.
    """
    if not icc:
        return img, False
    try:
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
    except (OSError, ImageCms.PyCMSError):
        return img, False
    mode = "RGBA" if has_alpha(img) else "RGB"
    try:
        out = ImageCms.profileToProfile(
            img.convert(mode), src_profile, _SRGB, outputMode=mode, renderingIntent=ImageCms.Intent.PERCEPTUAL
        )
    except ImageCms.PyCMSError:
        return img, False
    assert out is not None
    return out, True


def make_renditions(data: bytes) -> list[Rendition]:
    with Image.open(io.BytesIO(data)) as src:
        src.load()
        base, managed = to_srgb(upright(src), src.info.get("icc_profile"))
        alpha = has_alpha(base)
        rgba = base.convert("RGBA") if alpha else None
        rgb = to_rgb(base)
    out = []
    for name, spec in RENDITION_SPECS.items():
        img = (rgba if spec.format == "webp" and rgba is not None else rgb).copy()
        img.thumbnail((spec.max_px, spec.max_px), Image.Resampling.LANCZOS)  # never upscales
        buf = io.BytesIO()
        pil_format = FORMATS[spec.format][0]
        if pil_format == "JPEG":
            img.save(buf, "JPEG", quality=spec.quality, optimize=True, progressive=True)
        else:
            img.save(buf, "WEBP", quality=spec.quality, method=6)
        out.append(Rendition(name, spec, buf.getvalue(), img.width, img.height, managed))
    return out
