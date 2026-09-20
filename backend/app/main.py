"""HTTP API over the same search path the CLI uses.

    cd backend && uv run uvicorn app.main:app --reload

Endpoints are `def`, NEVER `async def`: search() and parse_query() block on
network I/O, so declared async they would run on the event loop and serialise
every request behind the slowest one. As plain `def` they go to FastAPI's
threadpool, which is safe because session_scope() builds a fresh Session per
call and httpx.Client is thread-safe.
"""

import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import session_scope
from app.embedding import embed_query
from app.explain import explain
from app.games import get_game
from app.metrics import note, record, snapshot
from app.query_parser import parse_query
from app.rerank import RerankUnavailable, rerank_scores
from app.schemas import (
    ExplainRequest,
    ExplainResponse,
    GameDetail,
    SearchRequest,
    SearchResponse,
    StatsResponse,
)
from app.search import search

# Configured here so the parser's fallback WARNING reaches the console under
# uvicorn rather than being swallowed: the fallback AND the log line.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)


def _warm_models() -> None:
    """Load every model a search needs before a user asks for one.

    Cold: 21.9s for the first search against 0.8s warm, and the cross-encoder
    adds 8.8s on top - or a 2.4GB download on a new machine. Warmed separately
    from Ollama so neither failure hides the other, and only under
    RANK_METHOD=rerank, because the container has no torch to import.

    Cold loads do not disappear: OLLAMA_KEEP_ALIVE evicts an idle model and the
    next search pays again. This moves the cost off the first user.
    """
    started = time.perf_counter()
    try:
        embed_query("warmup")
        parse_query("warmup")
    except Exception:
        # Never fatal: the API is still useful, the first search is just slow.
        logger.warning("model warmup failed; first search will be slow", exc_info=True)
    else:
        logger.info("models warm in %.1fs", time.perf_counter() - started)

    if settings.rank_method != "rerank":
        return
    started = time.perf_counter()
    try:
        # The public entry point, so this loads exactly what a search would, and
        # a search arriving meanwhile waits on the loader's lock.
        rerank_scores("warmup", ["warmup"])
    except RerankUnavailable as exc:
        # Remembered by the loader, so searches fall back without retrying it.
        logger.warning("cross-encoder did not load; searches will use rrf: %s", exc)
    else:
        logger.info("cross-encoder warm in %.1fs", time.perf_counter() - started)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # In a thread, so uvicorn binds the port immediately rather than sitting
    # unavailable for 20s. Daemon, so it cannot hold up shutdown.
    threading.Thread(target=_warm_models, daemon=True).start()
    yield


app = FastAPI(
    title="Steam Vibe Search",
    description="Semantic search over ~139k Steam games, with structured filters.",
    version="0.1.0",
    lifespan=lifespan,
)

# Both spellings of Vite's dev server, and explicit rather than "*". On Windows
# the browser needs localhost and the API needs 127.0.0.1.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, object]:
    """Liveness plus enough detail to tell which dependency is down."""
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        database = "ok"
    except SQLAlchemyError:
        logger.exception("health check: database unreachable")
        database = "unreachable"

    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "embed_model": settings.embed_model,
        "chat_model": settings.chat_model,
    }


@app.post("/api/search")
def search_endpoint(request: SearchRequest) -> SearchResponse:
    """Natural language in, ranked games out.

    Without `parsed`, the chat model extracts filters. With it, they are used
    verbatim and no model runs - the editable-chip path, where re-parsing would
    re-derive the chip the user just deleted.
    """
    started = time.perf_counter()

    if request.parsed is not None:
        parsed = request.parsed
        parse_ms: float | None = None
    else:
        parse_start = time.perf_counter()
        # Never raises: a parse failure returns the raw text with no filters,
        # which is exactly how search behaved before the parser existed.
        parsed = parse_query(request.query)
        parse_ms = (time.perf_counter() - parse_start) * 1000

    try:
        response = search(
            parsed,
            limit=request.limit,
            threshold=request.threshold,
            relax_filters=request.relax,
        )
    except httpx.HTTPError as exc:
        # Embedding is not optional the way parsing is: without a query vector
        # there is nothing to rank.
        logger.exception("embedding request failed for %r", parsed.semantic_query)
        # Counted because a 503 records no timings, and /api/stats would
        # otherwise look healthy while every search failed.
        note("search_failed_embedding")
        raise HTTPException(
            status_code=503, detail="Embedding service unavailable."
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("search query failed for %r", parsed.semantic_query)
        note("search_failed_database")
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc

    response.parse_ms = parse_ms
    _record_search(response, started)
    return response


def _record_search(response: SearchResponse, started: float) -> None:
    """One log line and one metrics record per search.

    At the ENDPOINT, not inside search(), which keeps the ranking path
    undiluted; the cost is that the CLI and run_eval record nothing, so
    /api/stats covers API traffic only. A stage that did not run is OMITTED,
    never zeroed - a 0.0 would read as a stage that costs nothing.
    """
    total_ms = (time.perf_counter() - started) * 1000
    timings = {"total_ms": total_ms}
    for stage in ("parse_ms", "relax_ms", "embed_ms", "query_ms", "rerank_ms"):
        value = getattr(response, stage)
        if value is not None:
            timings[stage] = value
    record("search", timings)

    # Read off the response, because each of these already travels on it. The
    # parse counters are the exception and live in app/query_parser.py, whose
    # fallback leaves no trace. rerank_ms set with reranked False means the
    # model was ASKED and failed - failures.md #36.
    if response.rerank_ms is not None and not response.reranked:
        note("rerank_fell_back")
    if response.relaxed:
        note("relaxed")
    if response.under_delivered:
        note("under_delivered")

    # One line, every stage, so a slow search can be attributed from the log
    # without reproducing it.
    logger.info(
        "search %r -> %d results in %.0fms (parse %s, relax %s, embed %.0f, "
        "query %.0f, rerank %s)%s",
        response.parsed.semantic_query,
        response.returned,
        total_ms,
        "-" if response.parse_ms is None else f"{response.parse_ms:.0f}",
        "-" if response.relax_ms is None else f"{response.relax_ms:.0f}",
        response.embed_ms,
        response.query_ms,
        "-" if response.rerank_ms is None else f"{response.rerank_ms:.0f}",
        " RELAXED" if response.relaxed else "",
    )


@app.post("/api/explain")
def explain_endpoint(request: ExplainRequest) -> ExplainResponse:
    """One "why this matches" line per game, verified against the database.

    A SECOND request on purpose: an LLM call inline would hold the whole result
    list for a sentence nobody has scrolled to. `query` must be
    `parsed.semantic_query`, never the typed text - see ExplainRequest.

    Never 503s: a dead model degrades to a deterministic line marked
    `grounded=False`.
    """
    explanations, elapsed_ms = explain(
        request.query, request.app_ids, wanted_tags=request.wanted_tags
    )

    record("explain", {"total_ms": elapsed_ms})
    # The discard rate is this feature's deliverable. Counted per EXPLANATION,
    # not per request, which would overstate it tenfold.
    ungrounded = sum(1 for item in explanations if not item.grounded)
    if ungrounded:
        note("explanations_ungrounded", ungrounded)
    note("explanations_total", len(explanations))

    logger.info(
        "explain %d games in %.0fms (%d ungrounded)",
        len(request.app_ids),
        elapsed_ms,
        ungrounded,
    )
    return ExplainResponse(explanations=explanations, elapsed_ms=elapsed_ms)


@app.get("/api/stats")
def stats_endpoint() -> StatsResponse:
    """Latency percentiles and fallback counts for this process.

    Read the scope fields in the body before quoting anything: `window` counts
    REQUESTS, the numbers are per-process, and they cover API traffic only.
    p95 is null below `min_p95_samples`, where it would be the maximum.
    """
    return snapshot()


@app.get("/api/game/{app_id}")
def game_endpoint(app_id: int) -> GameDetail:
    """One game. 404 when the id does not exist."""
    game = get_game(app_id)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No game with app_id {app_id}.")
    return game
