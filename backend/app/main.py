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
import time

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import session_scope
from app.games import get_game
from app.query_parser import parse_query
from app.schemas import GameDetail, SearchRequest, SearchResponse
from app.search import search

# The parser degrades to semantic-only search on failure and logs a WARNING
# saying so. Configured here so that line reaches the console under uvicorn
# rather than being swallowed - CLAUDE.md wants the fallback AND the log line.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Steam Vibe Search",
    description="Semantic search over ~139k Steam games, with structured filters.",
    version="0.1.0",
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
        response = search(parsed, limit=request.limit, threshold=request.threshold)
    except httpx.HTTPError as exc:
        # Embedding is not optional the way parsing is - without a query vector
        # there is nothing to rank. Report it as a dependency failure rather
        # than a traceback.
        logger.exception("embedding request failed for %r", parsed.semantic_query)
        raise HTTPException(
            status_code=503, detail="Embedding service unavailable."
        ) from exc
    except SQLAlchemyError as exc:
        logger.exception("search query failed for %r", parsed.semantic_query)
        raise HTTPException(status_code=503, detail="Database unavailable.") from exc

    response.parse_ms = parse_ms
    return response


@app.get("/api/game/{app_id}")
def game_endpoint(app_id: int) -> GameDetail:
    """One game. 404 when the id does not exist."""
    game = get_game(app_id)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No game with app_id {app_id}.")
    return game
