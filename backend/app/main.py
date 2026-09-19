"""HTTP API over the same search path the CLI uses.

    cd backend && uv run uvicorn app.main:app --reload

Endpoints are declared `def`, NOT `async def`, and that is deliberate. search()
and parse_query() are synchronous and both block on network I/O - Ollama over
HTTP plus a Postgres round-trip. Declared async they would run directly on the
event loop and serialise every request behind the slowest one. As plain `def`,
FastAPI hands them to its threadpool. session_scope() builds a fresh Session
per call and httpx.Client is thread-safe, so the existing code is already
correct under that model.
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

# The parser degrades to semantic-only search on failure and logs a WARNING
# saying so. Configured here so that line reaches the console under uvicorn
# rather than being swallowed - CLAUDE.md wants the fallback AND the log line.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)


def _warm_models() -> None:
    """Load every model a search needs before a user asks for one.

    Measured cold: 21.9s for the first search, against 0.8s warm - almost all
    of it loading 6.6GB of chat model plus the embedder. In a browser a 22s
    spinner is indistinguishable from a hang.

    The cross-encoder is warmed too, and was not until a fresh-clone check. This
    function predates stage 3, so the first search after every restart paid the
    reranker's load plus its full-shape warm-up - 8,822ms of a 10.9s search -
    and on a new machine a 2.4GB download on top: 124s, behind a UI hint that
    promises about 20. Warmed separately from Ollama so that neither failure
    hides the other, and only under RANK_METHOD=rerank: the container runs rrf
    and has no torch to import.

    This does not make cold loads disappear. OLLAMA_KEEP_ALIVE is 30m, so an
    idle server evicts and the next search pays again; the frontend says so
    while it waits. It moves the cost off the first user, which is where it
    is most damaging.
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
        # The public entry point, so this loads exactly what a search would:
        # _load() downloads if needed, then warms at the full pool shape. A
        # search arriving meanwhile waits on the loader's lock rather than
        # loading a second copy.
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

# Vite's dev server. Listed explicitly rather than "*" - the API is read-only
# today, but a wildcard is a habit worth not forming.
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

    Two paths. Without `parsed`, the chat model extracts filters from `query`.
    With it, those filters are used verbatim and no model runs - that is the
    editable-chip path, and re-parsing there would re-derive whichever chip the
    user just deleted.
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
        # Embedding is not optional the way parsing is - without a query vector
        # there is nothing to rank. Report it as a dependency failure rather
        # than a traceback.
        logger.exception("embedding request failed for %r", parsed.semantic_query)
        # Counted, because a 503 records no timings and would otherwise leave
        # /api/stats looking healthy while every search failed. These are the
        # only counters not read off a response - there is no response.
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

    Deliberately at the ENDPOINT rather than inside search(), which keeps the
    ranking path undiluted - the same reason app/games.py is not part of
    app/search.py. The honest cost is that the CLI and run_eval record nothing,
    so /api/stats describes API traffic only and is not comparable with the
    median run_eval prints.

    A stage that did not run is OMITTED, never zeroed. parse_ms is None on the
    chip path and rerank_ms is None wherever RANK_METHOD is not `rerank`;
    recording either as 0.0 would drag that stage's p50 toward nothing and read
    as a stage that costs nothing.
    """
    total_ms = (time.perf_counter() - started) * 1000
    timings = {"total_ms": total_ms}
    for stage in ("parse_ms", "relax_ms", "embed_ms", "query_ms", "rerank_ms"):
        value = getattr(response, stage)
        if value is not None:
            timings[stage] = value
    record("search", timings)

    # The counters below are read off the response rather than raised at the
    # point of failure, because every one of these already travels on it.
    # `parse_call_failed` and `parse_bad_output` are the exceptions and are
    # counted inside app/query_parser.py - a parse fallback returns a bare
    # ParsedQuery and leaves no other trace.
    #
    # rerank_ms set with reranked False means the cross-encoder was ASKED and
    # failed, which is failures.md #36: the search still returns the SQL
    # ordering while every label on it says `rerank`.
    if response.rerank_ms is not None and not response.reranked:
        note("rerank_fell_back")
    if response.relaxed:
        note("relaxed")
    if response.under_delivered:
        note("under_delivered")

    # There was no per-request log line at all before this. At INFO, one line,
    # every stage - so a slow search can be attributed from the server log
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

    Separate from /api/search on purpose. Search already costs ~1.1s of
    reranking, and an LLM call inline would push a first result list past three
    seconds for something the user has not asked to read yet. The UI renders
    results, then fills these in.

    The body carries app_ids, NOT games. Name, tags and description are read
    server-side, because a verifier grading the model against caller-supplied
    tags would be checking the model against the caller.

    Never 503s. explain() degrades a dead model to a deterministic line marked
    `grounded=False`, which is the right answer for a feature that only
    decorates a result list that already works.
    """
    explanations, elapsed_ms = explain(
        request.query, request.app_ids, wanted_tags=request.wanted_tags
    )

    record("explain", {"total_ms": elapsed_ms})
    # The discard rate is this feature's deliverable, so it is the one number
    # here worth counting rather than timing. Counted per EXPLANATION, not per
    # request - a request asking about ten games and failing one is not a failed
    # request, and rounding it to one would overstate the rate tenfold.
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

    Read the scope fields in the body before quoting anything from it. `window`
    is a count of REQUESTS, not a period of time; the numbers cover this process
    only and reset on restart; and they see API traffic only, so they are not
    the same measurement as the median run_eval prints.

    p95 is null until `min_p95_samples` requests have been recorded, because
    below that the 95th percentile IS the maximum and printing the maximum
    under a p95 label is a wrong label rather than an imprecise number.

    An in-memory read behind a lock held only for the copy, so the UI can call
    it after every search without competing with searches.
    """
    return snapshot()


@app.get("/api/game/{app_id}")
def game_endpoint(app_id: int) -> GameDetail:
    """One game. 404 when the id does not exist."""
    game = get_game(app_id)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No game with app_id {app_id}.")
    return game
