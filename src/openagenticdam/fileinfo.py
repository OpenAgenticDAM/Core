"""Human-facing file information for an asset: UUID, name, format, resolution, size, dates."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    asset_id: str = Field(description="Permanent UUID of this asset in OpenAgenticDAM (UUID v4)")
    file_name: str
    format: str | None = Field(description="Detected container format, e.g. JPEG")
    mime_type: str
    width: int | None
    height: int | None
    resolution: str | None = Field(description='e.g. "1200 × 900 px"')
    megapixels: float | None
    file_size_bytes: int
    file_size: str = Field(description='Human readable, e.g. "1.4 MB"')
    created_at: str | None = Field(
        description="When the image was made: EXIF capture time (camera local time, no zone) "
        "or, failing that, the file's modification time"
    )
    created_at_source: Literal["exif", "file"] | None
    uploaded_at: str = Field(description="When the asset was added to OpenAgenticDAM (ISO 8601, UTC)")


def human_size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def build_file_info(
    asset_id: uuid.UUID | str,
    file_name: str,
    mime_type: str,
    size: int,
    tech: dict[str, Any],
    uploaded_at: datetime,
) -> FileInfo:
    w, h = tech.get("width"), tech.get("height")
    created: str | None = None
    source: Literal["exif", "file"] | None = None
    if tech.get("captured_at"):
        created, source = tech["captured_at"], "exif"
    elif tech.get("file_modified_at"):
        created, source = tech["file_modified_at"], "file"
    return FileInfo(
        asset_id=str(asset_id),
        file_name=file_name,
        format=tech.get("format"),
        mime_type=mime_type,
        width=w,
        height=h,
        resolution=f"{w} × {h} px" if w and h else None,
        megapixels=round(w * h / 1_000_000, 1) if w and h else None,
        file_size_bytes=size,
        file_size=human_size(size),
        created_at=created,
        created_at_source=source,
        uploaded_at=uploaded_at.isoformat(),
    )


def describe(info: FileInfo) -> str:
    """One-line German summary for preview labels."""
    parts = [info.file_name]
    if info.resolution:
        parts.append(info.resolution)
    parts += [info.format or info.mime_type, info.file_size]
    if info.created_at:
        parts.append(f"erstellt {info.created_at.replace('T', ' ')[:16]}")
    parts.append(f"hochgeladen {info.uploaded_at.replace('T', ' ')[:16]} UTC")
    parts.append(f"UUID {info.asset_id}")
    return " · ".join(parts)
