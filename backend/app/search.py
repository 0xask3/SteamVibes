"""Hybrid search: structured filters in SQL, vibe ranking in pgvector.

The filters come from a ParsedQuery, built either by hand from CLI flags or by
app/query_parser.py from natural language. One code path either way.

Read the query construction carefully - this is the file where a mistake
produces plausible results forever rather than an error.
"""

import logging
import time
from typing import Any

from sqlalchemy import Float, Select, and_, cast, exists, func, not_, select, text
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from app.config import settings
from app.db import session_scope
from app.embedding import embed_query
from app.models import Game, GameCategory, GameTag
from app.relax import relax
from app.rerank import RerankUnavailable, rerank_scores
from app.schemas import (
    ParsedQuery,
    RelaxationStep,
    SearchResponse,
    SearchResult,
)

logger = logging.getLogger(__name__)

TOP_TAGS_SHOWN = 5

# log10 of the most-reviewed game (8,815,087), normalising the popularity term
# to roughly [0, 1]. A constant, not a subquery: the snapshot is static.
LOG10_MAX_REVIEWS = 6.95

# Steam's own categories, not community tags, and broader than `Multi-player`
# alone: 744 of 22,127 co-op games lack that category. `Remote Play Together` is
# deliberately absent - it is a streaming feature, so 637 Single-player games
# qualified for it. See failures.md #18.
MULTIPLAYER_CATEGORIES = (
    "Multi-player",
    "Co-op",
    "Online Co-op",
    "Shared/Split Screen",
    "PvP",
    "Online PvP",
)


def _multiplayer_exists() -> Select[tuple[int]]:
    return select(GameCategory.app_id).where(
        GameCategory.app_id == Game.app_id,
        GameCategory.category.in_(MULTIPLAYER_CATEGORIES),
    )


def _apply_filters(stmt: Select[tuple], parsed: ParsedQuery) -> Select[tuple]:
    """Add one WHERE clause per populated field. Absent fields add nothing."""
    if parsed.max_price_usd is not None:
        # Free games are stored as 0.00, so they pass a max-price filter.
        stmt = stmt.where(Game.list_price_usd <= parsed.max_price_usd)
    if parsed.min_price_usd is not None:
        stmt = stmt.where(Game.list_price_usd >= parsed.min_price_usd)

    # ANDed: ["mac", "linux"] means it must run on both.
    for platform in parsed.platforms:
        stmt = stmt.where(getattr(Game, platform).is_(True))

    if parsed.required_tags:
        # && (any-of), never @> (all-of): the tags are a coarse recall gate and
        # the vector does the discriminating. All-of scored 55.4% against
        # any-of's 60.0% and returned zero rows on three-tag queries, because no
        # game carries all three. See failures.md #33.
        stmt = stmt.where(Game.tags.overlap(parsed.required_tags))
    if parsed.excluded_tags:
        # Negated overlap is "shares none of these". Untagged games have an
        # empty array rather than NULL, so they correctly pass.
        stmt = stmt.where(not_(Game.tags.overlap(parsed.excluded_tags)))

    if parsed.excluded_app_ids:
        stmt = stmt.where(Game.app_id.not_in(parsed.excluded_app_ids))

    if parsed.released_after is not None:
        stmt = stmt.where(
            Game.release_date >= func.make_date(parsed.released_after, 1, 1)
        )
    if parsed.max_required_age is not None:
        stmt = stmt.where(Game.required_age <= parsed.max_required_age)

    if parsed.multiplayer is True:
        stmt = stmt.where(exists(_multiplayer_exists()))
    elif parsed.multiplayer is False:
        stmt = stmt.where(not_(exists(_multiplayer_exists())))

    return stmt


def _unknown_tags(tags: list[str]) -> list[str]:
    """Tags that appear nowhere in the real vocabulary.

    Guards the hand-typed flag path: `Base Building` is not a tag,
    `Base-Building` is, and the wrong one returns zero rows with no error.
    """
    if not tags:
        return []
    with session_scope() as session:
        known = set(
            session.scalars(
                select(GameTag.tag).where(GameTag.tag.in_(tags)).distinct()
            ).all()
        )
    return [tag for tag in tags if tag not in known]


def _platforms(windows: bool, mac: bool, linux: bool) -> list[str]:
    names = (("Windows", windows), ("Mac", mac), ("Linux", linux))
    return [name for name, supported in names if supported]


def _rank_score(cand: Subquery) -> ColumnElement[float]:
    """Stage 2's ordering key, over the candidate pool stage 1 retrieved.

    Cosine carries no notion of prominence, so a 27-review game ties a
    73,524-review one for "city builder" (failures.md #24). `log` keeps
    magnitude but its weight is calibrated against one model's cosine spread;
    `rrf` reads only ranks and survives a model swap.
    """
    similarity = 1 - cand.c.dist
    if settings.rank_method == "none":
        return similarity

    weight = settings.popularity_weight
    if settings.rank_method == "log":
        # log10, not the raw count: reviews span five orders of magnitude. Cast
        # because Postgres' log() is numeric and the rest is double precision.
        popularity = cast(
            func.log(10, cand.c.total_reviews + 1) / LOG10_MAX_REVIEWS, Float
        )
        return similarity + weight * popularity

    # RRF over the candidate pool. `rerank` lands here too: the cross-encoder
    # replaces the COSINE rank inside this same sum later, so computing the
    # blend now costs nothing and is the ordering to fall back to.
    k = settings.rrf_k
    rank_by_similarity = func.row_number().over(order_by=cand.c.dist)
    rank_by_reviews = func.row_number().over(order_by=cand.c.total_reviews.desc())
    return cast(1.0 / (k + rank_by_similarity), Float) + cast(
        weight / (k + rank_by_reviews), Float
    )


def _ranks(values: list[float]) -> list[int]:
    """1-based rank per position, highest value first.

    Stable, so ties keep cosine order - the same tiebreak SQL's row_number()
    applies, which keeps the two orderings comparable.
    """
    order = sorted(range(len(values)), key=lambda i: -values[i])
    ranks = [0] * len(values)
    for position, index in enumerate(order, start=1):
        ranks[index] = position
    return ranks


def _rerank_pool(
    query: str, rows: list[Any]
) -> tuple[list[tuple[Any, float]], float, bool]:
    """Score the pool with the cross-encoder and re-fuse, in Python.

    The SAME rrf sum the SQL path uses - same k, same weight - with the model's
    rank substituted for the cosine one, which is what keeps a rerank run
    comparable to the `rrf` baseline instead of being a second variable.

    A model that will not load is not an outage: the rows are already ordered by
    that blend, so they are handed back untouched with a WARNING. The elapsed
    time is still reported, because time spent failing is time the user waited.
    """
    start = time.perf_counter()
    try:
        scores = rerank_scores(query, [row.embed_text or "" for row in rows])
    except RerankUnavailable as exc:
        logger.warning(
            "reranker unavailable, falling back to the %s ordering for %d "
            "candidates: %s",
            settings.rank_method,
            len(rows),
            exc,
        )
        elapsed = (time.perf_counter() - start) * 1000
        return [(row, float(row.rank_score)) for row in rows], elapsed, False

    k = settings.rrf_k
    weight = settings.popularity_weight
    by_model = _ranks(scores)
    by_reviews = _ranks([float(row.total_reviews or 0) for row in rows])
    fused = [
        1.0 / (k + by_model[i]) + weight / (k + by_reviews[i]) for i in range(len(rows))
    ]
    order = sorted(range(len(rows)), key=lambda i: -fused[i])
    elapsed = (time.perf_counter() - start) * 1000
    return [(rows[i], fused[i]) for i in order], elapsed, True


def search(
    parsed: ParsedQuery,
    limit: int = 10,
    threshold: int | None = None,
    relax_filters: bool | None = None,
) -> SearchResponse:
    """Filter in SQL, then rank what survives by embedding distance.

    threshold defaults to settings.review_threshold; passing it explicitly is
    how the eval sweeps values without editing a query.
    """
    # max(), not an override, so a "popular" request can only raise the floor.
    base_threshold = settings.review_threshold if threshold is None else threshold

    # Widen the filters BEFORE anything expensive. Runs on capped counts, never
    # on retried searches, so a relaxed query still pays one embed and one
    # rerank. `parsed` is rebound: from here down it means what was applied.
    should_relax = settings.relax_filters if relax_filters is None else relax_filters
    relaxed_steps: list[RelaxationStep] = []
    relax_ms: float | None = None
    if should_relax:
        # Timed like every other stage: a cost nobody can check is an assertion.
        relax_start = time.perf_counter()
        parsed, relaxed_steps = relax(
            parsed, limit, base_threshold, target=settings.relax_target_rows
        )
        relax_ms = (time.perf_counter() - relax_start) * 1000

    effective_threshold = max(base_threshold, parsed.min_reviews or 0)
    unknown = _unknown_tags(parsed.required_tags + parsed.excluded_tags)

    embed_start = time.perf_counter()
    query_vec = embed_query(parsed.semantic_query)
    embed_ms = (time.perf_counter() - embed_start) * 1000

    distance = Game.embedding.cosine_distance(query_vec)
    reranking = settings.rank_method == "rerank"

    pool_columns: list[Any] = [
        Game.app_id,
        Game.name,
        Game.short_description,
        distance.label("dist"),
        Game.list_price_usd,
        Game.is_free,
        Game.total_reviews,
        Game.positive_reviews,
        Game.windows,
        Game.mac,
        Game.linux,
        # tags is stored votes-first, so a slice is the top N. No subquery.
        Game.tags[1:TOP_TAGS_SHOWN].label("top_tags"),
    ]
    if reranking:
        # The exact text stage 1 indexed, so both stages score the same object.
        # ~1KB x 200 rows, which is why no other mode selects it.
        pool_columns.append(Game.embed_text.label("embed_text"))

    candidates = select(*pool_columns).where(
        and_(
            Game.embedding.isnot(None),
            Game.total_reviews > effective_threshold,
        )
    )
    candidates = _apply_filters(candidates, parsed)

    # Raw distance, not the derived score: only this form uses the index, and
    # the operator must stay cosine to match vector_cosine_ops. Any composite
    # expression here silently drops to an exact scan.
    cand = (
        candidates.order_by(distance).limit(settings.rerank_candidates).subquery("cand")
    )

    rank_score = _rank_score(cand)
    # "none" orders by raw distance, so the no-op default is the same
    # expression the single-stage query used rather than an equal one.
    ordering = cand.c.dist if settings.rank_method == "none" else rank_score.desc()

    stmt = select(
        *cand.c, (1 - cand.c.dist).label("score"), rank_score.label("rank_score")
    ).order_by(ordering)
    # The cross-encoder needs the WHOLE pool: reordering the ten rows stage 2
    # already picked cannot surface what it buried, and 9 of 9 tail misses sit
    # between rank 11 and 200.
    if not reranking:
        stmt = stmt.limit(limit)

    query_start = time.perf_counter()
    with session_scope() as session:
        # Not optional: HNSW filters after gathering, so a selective WHERE
        # silently returns fewer rows than asked for (4 of 10 at threshold
        # 5000). SET LOCAL keeps it off the pooled connection.
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        # Interpolated rather than bound - SET takes no parameters - with int()
        # as the guard. See config.hnsw_ef_search for why 800.
        session.execute(
            text(f"SET LOCAL hnsw.ef_search = {int(settings.hnsw_ef_search)}")
        )
        rows = session.execute(stmt).all()
    query_ms = (time.perf_counter() - query_start) * 1000

    # Stage 2 already ran in SQL; stage 3 overrides its ordering and scores.
    # Pairing rows with scores here lets both paths share one result builder.
    rerank_ms: float | None = None
    reranked = False
    scored: list[tuple[Any, float]] = [(row, float(row.rank_score)) for row in rows]
    if reranking:
        scored, rerank_ms, reranked = _rerank_pool(parsed.semantic_query, list(rows))
    scored = scored[:limit]

    results = [
        SearchResult(
            app_id=row.app_id,
            name=row.name,
            short_description=row.short_description,
            score=float(row.score),
            rank_score=rank_value,
            list_price_usd=row.list_price_usd,
            is_free=row.is_free,
            total_reviews=row.total_reviews,
            positive_ratio=(
                row.positive_reviews / row.total_reviews if row.total_reviews else None
            ),
            tags=list(row.top_tags or []),
            platforms=_platforms(row.windows, row.mac, row.linux),
        )
        for row, rank_value in scored
    ]

    return SearchResponse(
        parsed=parsed,
        results=results,
        unknown_tags=unknown,
        requested=limit,
        returned=len(results),
        threshold=effective_threshold,
        relaxed=relaxed_steps,
        relax_ms=relax_ms,
        embed_ms=embed_ms,
        query_ms=query_ms,
        rerank_ms=rerank_ms,
        reranked=reranked,
    )
