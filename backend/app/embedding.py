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


def _reason(response: httpx.Response) -> str:
    """Ollama's own explanation, which raise_for_status() throws away.

    The message is the whole diagnosis: "input (3002 tokens) is too large to
    process" points at batching, where a bare 400 sent us looking for a corrupt
    row that did not exist. Falls back to the raw body, because an error page is
    still more informative than a status code.
    """
    try:
        message = response.json().get("error")
    except ValueError:
        message = None
    return str(message or response.text or "<empty body>").strip()[:300]


def _post_batch(inputs: list[str]) -> list[list[float]]:
    """Embed one batch, halving it if the server rejects the batch as too large.

    Ollama packs several inputs into a single server task, and it is the PACKED
    token count that gets checked against the physical batch size - so a request
    of perfectly ordinary rows can be rejected while every text in it is tiny.
    settings.embed_num_batch raises that ceiling to the model's context; this
    halving is the net for anything above it, and for a ceiling that turns out
    to be model-specific.

    Splitting is safe because embeddings are per-input and order is preserved:
    the only cost is an extra round trip. A single input that still fails raises,
    because at that point it really is the text.

    Retries the batch, never the whole run: ingest is resumable, but a 30-minute
    job should not die on a transient. The WARNING is the point - a silent split
    would hide a model that cannot handle the corpus at all.
    """
    response = _client.post(
        "/api/embed",
        json={
            "model": settings.embed_model,
            "input": inputs,
            # Without this Ollama evicts the model after 5 minutes idle, and the
            # next query spends ~18s reloading it to do ~20ms of work.
            "keep_alive": settings.ollama_keep_alive,
            "options": {"num_batch": settings.embed_num_batch},
        },
    )

    if response.status_code == 400 and len(inputs) > 1:
        half = len(inputs) // 2
        logger.warning(
            "Ollama rejected a batch of %d (%s). Retrying as %d + %d.",
            len(inputs),
            _reason(response),
            half,
            len(inputs) - half,
        )
        return _post_batch(inputs[:half]) + _post_batch(inputs[half:])

    if response.is_error:
        raise RuntimeError(
            f"{settings.embed_model} rejected {len(inputs)} input(s) with HTTP "
            f"{response.status_code}: {_reason(response)}"
        )

    vectors: list[list[float]] = response.json()["embeddings"]
    return vectors


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
    vectors = _post_batch([prefix + text for text in texts] if prefix else texts)

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


@cache
def verify_corpus_model() -> None:
    """Fail loudly if the corpus was embedded by a model other than EMBED_MODEL.

    Nothing else catches this. `embed_texts` checks the dimension, but arctic,
    bge-m3, qwen3-embedding and the column are all 1024, so a mismatched
    EMBED_MODEL sails straight through it: the query gets embedded in one space
    and compared against documents in another. There is no exception and no
    empty result set, just quietly worse rankings forever - the same shape as
    failures.md #13 and #22, and the reason _MODEL_PREFIXES is keyed on the
    model name instead of being two settings that can drift.

    The window is exactly when the model is being changed. Switching is a .env
    line plus `alembic downgrade 0006`, `embed_all --reload` and
    `alembic upgrade head`; stopping between any two of those leaves the halves
    disagreeing, and the eval would report the difference as a model result.

    A half-finished re-embed trips this too, which is correct: a corpus split
    across two models cannot be ranked against either one.

    Cached, so this is one query per process rather than one per search.
    """
    from sqlalchemy import distinct, select

    from app.db import session_scope
    from app.models import Game

    with session_scope() as session:
        found = {
            model
            for model in session.scalars(
                select(distinct(Game.embedding_model)).where(Game.embedding.isnot(None))
            )
            if model is not None
        }

    # Nothing embedded yet is not this function's problem - embed_all says so
    # far more usefully, and the API should still start against an empty corpus.
    if not found or found == {settings.embed_model}:
        return

    listed = ", ".join(sorted(found))
    raise SystemExit(
        f"EMBED_MODEL is {settings.embed_model!r} but the corpus was embedded "
        f"by {listed}. Queries would be embedded in a different space than the "
        "documents, which returns worse results with no error. Either set "
        "EMBED_MODEL back, or re-embed: alembic downgrade 0006, "
        "embed_all --reload, alembic upgrade head."
    )


@cache
def verify_corpus_complete() -> None:
    """Refuse to produce a results table from a half-embedded corpus.

    verify_corpus_model() catches the WRONG model. This catches the right model
    applied to only part of the table, which that check cannot see: during a
    `--reload` every vector is cleared and refilled over ~25 minutes, and the
    model column agrees with EMBED_MODEL the whole time. Search runs happily
    against whatever fraction exists, returning fewer and worse results with no
    error - the same silent shape as the mismatch, from a different cause.

    Only the eval calls this. A partial corpus is a normal state to search from
    while ingest is running, and the CLI should stay usable; a recall number
    measured mid-reload is not merely imprecise, it looks exactly like a model
    result and would be written into a table as one.
    """
    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import Game

    with session_scope() as session:
        pending = session.scalar(
            select(func.count())
            .select_from(Game)
            .where(Game.embed_text.isnot(None), Game.embedding.is_(None))
        )

    if pending:
        raise SystemExit(
            f"{pending:,} games have embed_text but no vector, so the corpus is "
            "incomplete - probably an embed_all run in progress. Recall measured "
            "now would be against a fraction of the corpus and would read as a "
            "config result. Wait for `embed_all` to report 0 pending."
        )


def embed_query(text: str) -> list[float]:
    """Embed a single search query."""
    return embed_texts([text], is_query=True)[0]
