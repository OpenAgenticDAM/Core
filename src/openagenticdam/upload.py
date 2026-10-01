"""Chunked uploads from an MCP App (chat upload).

MCP has no file-upload primitive, and a tool call from the model cannot carry the bytes of
a file the user dropped into the chat. An MCP App (sandboxed iframe in the chat) can: it
reads the file locally and streams it to the server through app-only tools in chunks of
UPLOAD_CHUNK_BYTES (hosts limit message sizes).

Staging lives on local disk, keyed by a server-generated UUID; tenant and actor are bound
to the upload at `begin` and checked on every later call.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import pillow_heif
from PIL import Image

UPLOAD_CHUNK_BYTES = 512 * 1024
UPLOAD_TTL_SECONDS = 3600
MAX_PIXELS = 100_000_000  # explicit decompression-bomb ceiling (Pillow's default warns at ~89 MP)

ALLOWED_FORMATS = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "TIFF": "image/tiff",
    "GIF": "image/gif",
    "HEIF": "image/heif",  # HEIC from iPhones; pillow-heif reports both as HEIF
}

pillow_heif.register_heif_opener()


class UploadError(Exception):
    """Anticipated upload failure. `code` is the stable error code shown to the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass
class UploadMeta:
    upload_id: str
    tenant_id: str
    actor: str
    file_name: str
    size_bytes: int
    received: int
    created_at: float


def safe_file_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .") or "upload"
    return base[:120]


class UploadStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    # --- paths -------------------------------------------------------------------------
    def _paths(self, upload_id: str) -> tuple[Path, Path]:
        try:
            canonical = str(uuid.UUID(upload_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise UploadError("unknown_upload", "no such upload") from exc
        if canonical != upload_id:
            raise UploadError("unknown_upload", "no such upload")
        return self.root / f"{canonical}.part", self.root / f"{canonical}.json"

    def _load(self, upload_id: str, tenant_id: str, actor: str) -> UploadMeta:
        part, meta_path = self._paths(upload_id)
        if not meta_path.exists():
            raise UploadError("unknown_upload", "no such upload")
        meta = UploadMeta(**json.loads(meta_path.read_text()))
        # Foreign uploads look exactly like missing ones.
        if meta.tenant_id != tenant_id or meta.actor != actor:
            raise UploadError("unknown_upload", "no such upload")
        if time.time() - meta.created_at > UPLOAD_TTL_SECONDS:
            self.discard(upload_id)
            raise UploadError("unknown_upload", "upload expired")
        return meta

    def _save(self, meta: UploadMeta) -> None:
        _, meta_path = self._paths(meta.upload_id)
        tmp = meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(meta)))
        tmp.replace(meta_path)

    # --- lifecycle ---------------------------------------------------------------------
    def begin(self, tenant_id: str, actor: str, file_name: str, size_bytes: int) -> UploadMeta:
        self.purge_expired()
        if size_bytes <= 0:
            raise UploadError("invalid_argument", "size_bytes must be positive")
        if size_bytes > self.max_bytes:
            raise UploadError("too_large", f"file exceeds the limit of {self.max_bytes // (1024 * 1024)} MB")
        free = shutil.disk_usage(self.root).free
        if free < size_bytes * 2:
            raise UploadError("storage_full", "not enough free disk space for staging")
        meta = UploadMeta(
            upload_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            actor=actor,
            file_name=safe_file_name(file_name),
            size_bytes=size_bytes,
            received=0,
            created_at=time.time(),
        )
        part, _ = self._paths(meta.upload_id)
        part.touch(mode=0o600)
        self._save(meta)
        return meta

    def append(self, upload_id: str, tenant_id: str, actor: str, offset: int, data: bytes) -> UploadMeta:
        meta = self._load(upload_id, tenant_id, actor)
        if offset != meta.received:
            raise UploadError("bad_offset", f"expected offset {meta.received}, got {offset}")
        if not data or len(data) > UPLOAD_CHUNK_BYTES:
            raise UploadError("invalid_argument", f"chunk must be 1..{UPLOAD_CHUNK_BYTES} bytes")
        if meta.received + len(data) > meta.size_bytes:
            raise UploadError("too_large", "more data than declared")
        part, _ = self._paths(upload_id)
        with part.open("ab") as f:
            f.write(data)
        meta.received += len(data)
        self._save(meta)
        return meta

    def finish(self, upload_id: str, tenant_id: str, actor: str) -> tuple[UploadMeta, bytes, str, tuple[int, int]]:
        """Return (meta, bytes, mime, (w, h)) of a complete, validated image. Discards the staging files."""
        meta = self._load(upload_id, tenant_id, actor)
        part, _ = self._paths(upload_id)
        try:
            if meta.received != meta.size_bytes:
                raise UploadError("incomplete", f"received {meta.received} of {meta.size_bytes} bytes")
            data = part.read_bytes()
            mime, dims = validate_image(data)
            return meta, data, mime, dims
        finally:
            self.discard(upload_id)

    def discard(self, upload_id: str) -> None:
        part, meta_path = self._paths(upload_id)
        part.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)

    def purge_expired(self) -> None:
        now = time.time()
        for meta_path in self.root.glob("*.json"):
            try:
                created = json.loads(meta_path.read_text())["created_at"]
            except (OSError, ValueError, KeyError):
                created = 0
            if now - created > UPLOAD_TTL_SECONDS:
                meta_path.with_suffix(".part").unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)


def validate_image(data: bytes) -> tuple[str, tuple[int, int]]:
    """Content sniffing - the file name and the client's MIME type are not trusted."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or ""
            w, h = img.size
            if fmt not in ALLOWED_FORMATS:
                raise UploadError("invalid_image", f"unsupported format {fmt or 'unknown'}")
            if w * h > MAX_PIXELS:
                raise UploadError("invalid_image", "image dimensions too large")
            img.verify()
    except UploadError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise UploadError("invalid_image", "file is not a readable image") from exc
    return ALLOWED_FORMATS[fmt], (w, h)
