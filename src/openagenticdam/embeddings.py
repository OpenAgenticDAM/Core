"""Embeddings via a local Ollama instance (local-first; cloud providers are opt-in later)."""

from __future__ import annotations

import httpx

from openagenticdam.config import Settings


class EmbeddingError(RuntimeError):
    pass


def embed_text(settings: Settings, text: str) -> list[float]:
    try:
        r = httpx.post(
            f"{settings.ollama_url}/api/embed",
            json={"model": settings.embed_model, "input": text},
            timeout=30,
        )
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"embedding backend unavailable: {exc}") from exc
    vec = r.json()["embeddings"][0]
    if len(vec) != settings.embed_dim:
        raise EmbeddingError(f"model {settings.embed_model} returned dim {len(vec)}, expected {settings.embed_dim}")
    return vec
