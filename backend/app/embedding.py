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

# (query prefix, document prefix), by model name with the ":tag" stripped, and
# keyed on the model rather than set in .env on purpose: the document prefix is
# baked into every stored vector while the query prefix is applied live, so two
# settings could drift into embedding queries in a different space than the
# corpus - worse results forever, with no error. Each is from the model's card.
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
    prefix of "nomic-embed-text-v2-moe" and they are different models. An
    unknown model is not an error, but it gets a log line, because a typo in
    EMBED_MODEL looks exactly like a model that wants no prefix. Cached so that
    warning appears once per process.
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
    process" points at batching, where a bare 400 cost an hour of looking for a
    corrupt row that did not exist.
    """
    try:
        message = response.json().get("error")
    except ValueError:
        message = None
    return str(message or response.text or "<empty body>").strip()[:300]


def _post_batch(inputs: list[str]) -> list[list[float]]:
    """Embed one batch, halving it if the server rejects it as too large.

    Ollama checks the PACKED token count of several inputs against the physical
    batch, so ordinary rows can be rejected while every text in them is tiny
    (NOTES.md 2026-09-06). Splitting is safe - embeddings are per-input and
    order is preserved - and a single input that still fails raises, because
    then it really is the text. The WARNING is load-bearing: a silent split
    would hide a model that cannot handle the corpus at all.
    """
    response = _client.post(
        "/api/embed",
        json={
            "model": settings.embed_model,
            "input": inputs,
            # Without this Ollama evicts the model after 5 minutes idle.
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

    `is_query` picks which side of the asymmetry this is, defaulting to the
    document side: that is the bulk path, and a document embedded as a query
    would corrupt the whole corpus. Raises rather than returning partial
    results, which would misalign vectors against their rows.
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

    Nothing else catches this: arctic, bge-m3, qwen3-embedding and the column
    are all 1024 dims, so the dimension check passes and the query is simply
    compared against documents from a different space - no exception, no empty
    result, just quietly worse rankings. A half-finished re-embed trips it too,
    which is correct. Cached: one query per process.
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

    # An empty corpus is not this function's problem: the API must still start.
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

    The sibling of verify_corpus_model(), catching what it cannot: the RIGHT
    model applied to only part of the table, which the model column cannot show
    during a ~22-minute reload.

    Only the eval calls this, on purpose. Searching a partial corpus is normal
    while ingest runs; a recall number measured then is not imprecise, it looks
    exactly like a config result and would be written down as one.
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
