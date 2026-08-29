"""Ollama embedding client.

One long-lived httpx.Client, 127.0.0.1, batched /api/embed. All three matter:
reconnecting per call and resolving `localhost` together cost a 125x slowdown
on Windows. See NOTES.md, 2026-08-20.
"""

import httpx

from app.config import settings
from app.models import EMBEDDING_DIM

_REQUEST_TIMEOUT = 300.0

# Module-level and never closed: the process lifetime is the client lifetime,
# whether that process is a CLI invocation or a long-running API server.
_client = httpx.Client(base_url=settings.ollama_base_url, timeout=_REQUEST_TIMEOUT)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch in one round trip.

    Raises rather than returning partial results: a caller that silently gets
    fewer vectors than texts would misalign them against their rows.
    """
    if not texts:
        return []

    response = _client.post(
        "/api/embed",
        json={
            "model": settings.embed_model,
            "input": texts,
            # Without this Ollama evicts the model after 5 minutes idle, and the
            # next query spends ~18s reloading it to do ~20ms of work.
            "keep_alive": settings.ollama_keep_alive,
        },
    )
    response.raise_for_status()
    vectors: list[list[float]] = response.json()["embeddings"]

    if len(vectors) != len(texts):
        raise RuntimeError(
            f"sent {len(texts)} texts, got {len(vectors)} embeddings back"
        )
    if vectors and len(vectors[0]) != EMBEDDING_DIM:
        raise SystemExit(
            f"{settings.embed_model} returned {len(vectors[0])} dimensions, but "
            f"games.embedding is vector({EMBEDDING_DIM}). Changing the model "
            "requires a migration - see CLAUDE.md."
        )
    return vectors


def embed_query(text: str) -> list[float]:
    """Embed a single search query."""
    return embed_texts([text])[0]
