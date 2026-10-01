# syntax=docker/dockerfile:1
# Gateway + worker image. Small on purpose: no ML runtime inside - embeddings come from Ollama.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# ExifTool reads every metadata family (EXIF/XMP/IPTC/ICC/HEIF/maker notes). Debian package:
# perl + exiftool, no compiler. --no-install-recommends keeps the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libimage-exiftool-perl \
 && rm -rf /var/lib/apt/lists/* \
 && exiftool -ver

# Dependencies first (cached layer), project second.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

RUN useradd --system --uid 10001 oad
USER oad
EXPOSE 8000
