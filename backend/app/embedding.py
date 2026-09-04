"""Ollama embedding client.

One long-lived httpx.Client, 127.0.0.1, batched /api/embed. All three matter:
reconnecting per call and resolving `localhost` together cost a 125x slowdown
on Windows. See NOTES.md, 2026-08-20.

Retrieval models are asymmetric: the query and the document want different
prefixes, and getting that wrong costs recall silently. See _MODEL_PREFIXES.
"""

import logging
from functools import cache

import httpx

from app.config import settings
from app.models import EMBEDDING_DIM

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 300.0

# Module-level and never closed: the process lifetime is the client lifetime,
# whether that process is a CLI invocation or a long-running API server.
_client = httpx.Client(base_url=settings.ollama_base_url, timeout=_REQUEST_TIMEOUT)

# (query prefix, document prefix), by model name with the ":tag" stripped.
#
# Keyed on the model rather than exposed as two .env settings, and deliberately
# so: the document prefix is baked into every stored vector, while the query
# prefix is applied live. Two independent settings can drift apart, and the
# result is not an error - it is a query embedded in a slightly different space
# than the corpus, which just returns worse results forever. Keying on the model
# means picking EMBED_MODEL picks both halves, and they cannot disagree.
#
# Sources are each model's own card: arctic-embed wants "query: " and plain
# documents, bge-m3 wants neither side prefixed, qwen3-embedding wants an
# Instruct block on queries only, nomic wants both sides marked.
#
# qwen3-embedding's is a free-text task description rather than a fixed token,
# and it is applied to queries only - so it can be retuned and measured without
# re-embedding 130k documents.
_QWEN3_INSTRUCT = (
    "Instruct: Given a description of the feeling or content a player wants, "
    "retrieve video games matching it\nQuery: "
)

_MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    "snowflake-arctic-embed2": ("query: ", ""),
    "bge-m3": ("", ""),
    "qwen3-embedding": (_QWEN3_INSTRUCT, ""),
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "nomic-embed-text-v2-moe": ("search_query: ", "search_document: "),
}


@cache
def _prefixes(model: str) -> tuple[str, str]:
    """Look up (query, document) prefixes for a model name.

    Exact match on the base name, never startswith: "nomic-embed-text" is a
    prefix of "nomic-embed-text-v2-moe", and the two are different models that
    happen to share a convention.

    An unknown model is not an error - most embedding models take plain text -
    but it is worth a line in the log, because a typo in EMBED_MODEL looks
    exactly like a model that wants no prefix.

    Cached so the warning appears once per process rather than per batch.
    """
    base = model.split(":", 1)[0]
    found = _MODEL_PREFIXES.get(base)
    if found is None:
        logger.warning(
            "no prefix convention known for %r, sending text unprefixed. "
            "If this model expects one, add it to _MODEL_PREFIXES and re-embed.",
            model,
        )
        return ("", "")
    return found


def document_prefix() -> str:
    """The prefix baked into stored vectors. For ingest to report."""
    return _prefixes(settings.embed_model)[1]


def embed_texts(texts: list[str], *, is_query: bool = False) -> list[list[float]]:
    """Embed a batch in one round trip.

    `is_query` picks which side of the asymmetry we are on. It defaults to the
    document side because that is the bulk path (ingest), and because a document
    embedded as a query is the mistake that would corrupt the whole corpus -
    better that the rarer call site is the one that has to say so.

    Raises rather than returning partial results: a caller that silently gets
    fewer vectors than texts would misalign them against their rows.
    """
    if not texts:
        return []

    prefix = _prefixes(settings.embed_model)[0 if is_query else 1]

    response = _client.post(
        "/api/embed",
        json={
            "model": settings.embed_model,
            "input": [prefix + text for text in texts] if prefix else texts,
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
    return embed_texts([text], is_query=True)[0]
