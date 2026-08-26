"""Semantic search over the games table.

Weekend 1 is pure vector similarity plus the review-count filter. Weekend 2
adds the structured filters a parsed query produces (price, platform, tags,
year) to the same WHERE clause.

Read the SQL carefully - this is the file where a mistake produces plausible
results forever rather than an error.
"""

import time

from sqlalchemy import text

from app.config import settings
from app.db import session_scope
from app.embedding import embed_query
from app.schemas import SearchResponse, SearchResult

TOP_TAGS_SHOWN = 5

# hnsw.iterative_scan: HNSW gathers a fixed pool of candidates and only then
# applies WHERE. With a selective filter the pool empties and the query returns
# fewer rows than asked for - measured 4 of 10 at total_reviews > 5000, with no
# error. Iterative scan re-scans for more candidates until LIMIT is satisfied.
# strict_order keeps exact distance ordering; relaxed_order is faster but may
# return rows slightly out of order, which is not worth it for a ranking.
#
# SET LOCAL scopes this to the transaction so it cannot leak onto a pooled
# connection and quietly change the behaviour of unrelated queries.
_SEARCH_SQL = text(
    """
    SELECT g.app_id,
           g.name,
           g.short_description,
           1 - (g.embedding <=> :query_vec)      AS score,
           g.list_price_usd,
           g.is_free,
           g.total_reviews,
           g.positive_reviews,
           g.windows,
           g.mac,
           g.linux,
           (SELECT array_agg(t.tag ORDER BY t.votes DESC)
              FROM (SELECT tag, votes
                      FROM game_tags
                     WHERE app_id = g.app_id
                     ORDER BY votes DESC
                     LIMIT :top_tags) t)         AS tags
      FROM games g
     WHERE g.embedding IS NOT NULL
       AND g.total_reviews > :threshold
     -- Raw distance, not the derived score: only this form uses the index.
     -- The operator must stay <=> to match the index's vector_cosine_ops.
     ORDER BY g.embedding <=> :query_vec
     LIMIT :limit
    """
)


def _platforms(windows: bool, mac: bool, linux: bool) -> list[str]:
    names = (("Windows", windows), ("Mac", mac), ("Linux", linux))
    return [name for name, supported in names if supported]


def search(
    query: str, limit: int = 10, threshold: int | None = None
) -> SearchResponse:
    """Rank games by how close their embedded text is to `query`.

    threshold defaults to settings.review_threshold. Passing it explicitly is
    how Weekend 3 sweeps values without editing a query.
    """
    effective_threshold = (
        settings.review_threshold if threshold is None else threshold
    )

    embed_start = time.perf_counter()
    query_vec = embed_query(query)
    embed_ms = (time.perf_counter() - embed_start) * 1000

    query_start = time.perf_counter()
    with session_scope() as session:
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        rows = session.execute(
            _SEARCH_SQL,
            {
                # pgvector accepts the Postgres literal form for a vector.
                "query_vec": str(query_vec),
                "threshold": effective_threshold,
                "limit": limit,
                "top_tags": TOP_TAGS_SHOWN,
            },
        ).all()
    query_ms = (time.perf_counter() - query_start) * 1000

    results = [
        SearchResult(
            app_id=row.app_id,
            name=row.name,
            short_description=row.short_description,
            score=float(row.score),
            list_price_usd=row.list_price_usd,
            is_free=row.is_free,
            total_reviews=row.total_reviews,
            positive_ratio=(
                row.positive_reviews / row.total_reviews
                if row.total_reviews
                else None
            ),
            tags=list(row.tags or []),
            platforms=_platforms(row.windows, row.mac, row.linux),
        )
        for row in rows
    ]

    return SearchResponse(
        query=query,
        results=results,
        requested=limit,
        returned=len(results),
        threshold=effective_threshold,
        embed_ms=embed_ms,
        query_ms=query_ms,
    )
