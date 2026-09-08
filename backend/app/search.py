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

# log10 of the most-reviewed game in the corpus (8,815,087), which normalises
# the popularity term to roughly [0, 1]. A constant rather than a subquery: it
# is a property of a static Kaggle snapshot, and making every search pay a
# max() to learn something that cannot change would be silly.
LOG10_MAX_REVIEWS = 6.95

# Steam's own categories, not community tags: 12,643 games carry the `Co-op`
# category against 5,263 with the `Co-op` tag. The list is broader than
# `Multi-player` alone because 744 of 22,127 co-op/PvP games do not carry that
# category - filtering on it by itself would silently miss them.
#
# `Remote Play Together` is deliberately NOT here. It is a streaming feature,
# not a multiplayer mode: it sends one player's screen to a friend, so a
# Single-player game qualifies. Including it added 637 games that carry no
# real multiplayer category at all - HEXAROMA: Village Builder ranked 8th for
# "co-op base builder" on `Single-player, Remote Play Together` alone.
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
        # && against the GIN index: the row must carry at least ONE of these,
        # not all of them. It was `@>` (all-of) and that cost recall rather than
        # buying precision - measured over 118 queries, all-of scored 55.4%
        # against any-of's 60.0%, and of the 72 queries that got tags it helped
        # 3 and hurt 11. Six under-delivered and "running a bookshop and taking
        # on cosmic horror" returned ZERO rows on Cozy + Horror + Investigation,
        # because nothing carries all three.
        #
        # Any-of is the right shape for this filter: the vector does the
        # discriminating and the tags are a coarse recall gate ahead of it. It
        # does not weaken co-op or versus intent, which travels through
        # `multiplayer` and game_categories rather than through tags. Same GIN
        # index serves both operators, so this needed no migration.
        # See failures.md #33.
        stmt = stmt.where(Game.tags.overlap(parsed.required_tags))
    if parsed.excluded_tags:
        # && is "overlaps"; negated, "shares none of these". Games with no tags
        # have an empty array rather than NULL, so they correctly pass.
        stmt = stmt.where(not_(Game.tags.overlap(parsed.excluded_tags)))

    if parsed.excluded_app_ids:
        # "not including itself" after a referenced-game match. Set in code by
        # app/title_lookup.py, never by the model.
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

    Reported rather than left to return zero rows silently. `Base Building` is
    not a tag; `Base-Building` is.

    This guards the hand-typed flag path. Tags arriving from the parser are
    already exact - app/query_parser.py fuzzy-matches, then drops what it
    cannot resolve - so with --parse this list is normally empty.
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

    Cosine similarity carries no notion of prominence, and in a corpus that is
    58% games with 10 reviews or fewer that is not a small gap: `Square City
    Builder` (27 reviews) ties `Cities: Skylines II` (73,524) for "city
    builder". This adds one back, continuously - the review threshold already
    does it as a cliff, and buying recall by deleting 128,949 games is not the
    trade we want. See failures.md #24.

    Both methods are here so the eval can choose between them rather than the
    choice being argued. They differ in what they are sensitive to:

    `log` scores, so a much closer match keeps its margin - but the weight is
    only meaningful relative to the model's cosine spread, and that varies
    (qwen3's top 10 spans 0.752-0.696, arctic's 0.577-0.502). Change the model
    and the weight needs re-tuning.

    `rrf` ranks, so it is immune to that, at the cost of flattening magnitude:
    the closest match and the second closest are one rank apart whether they
    differ by 0.2 or 0.002.
    """
    similarity = 1 - cand.c.dist
    if settings.rank_method == "none":
        return similarity

    weight = settings.popularity_weight
    if settings.rank_method == "log":
        # log10 rather than raw count: reviews span five orders of magnitude,
        # so a linear term would make the top ~50 games the only ones that
        # exist. Cast because Postgres' log() is numeric and the rest of this
        # is double precision.
        popularity = cast(
            func.log(10, cand.c.total_reviews + 1) / LOG10_MAX_REVIEWS, Float
        )
        return similarity + weight * popularity

    # RRF. Ranks are within the candidate pool, which is the right frame: these
    # rows already passed retrieval, so the question is only how to order them.
    #
    # `rerank` lands here too, on purpose. The cross-encoder replaces the COSINE
    # rank inside this same sum, in Python, once the pool has been scored - so
    # computing this here costs nothing and buys the fallback: if TEI is down,
    # the rows are already ordered by the blend that shipped before it existed.
    k = settings.rrf_k
    rank_by_similarity = func.row_number().over(order_by=cand.c.dist)
    rank_by_reviews = func.row_number().over(order_by=cand.c.total_reviews.desc())
    return cast(1.0 / (k + rank_by_similarity), Float) + cast(
        weight / (k + rank_by_reviews), Float
    )


def _ranks(values: list[float]) -> list[int]:
    """1-based rank per position, highest value first.

    Stable, so equal values keep the order they arrived in - and they arrive in
    cosine order, which makes stage 1 the tiebreak. That is the same tiebreak
    SQL's row_number() applies, so the two orderings stay comparable.
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

    The fusion is the SAME rrf sum the SQL path uses - same k, same weight -
    with the cross-encoder's rank substituted for the cosine one. Deliberately
    not a new formula: keeping it identical is what makes a rerank run
    comparable to the `rrf w=0.20` baseline instead of being a second variable.
    POPULARITY_WEIGHT=0 collapses it to pure cross-encoder, which is how the
    eval prices the popularity term rather than assuming it survived.

    A dead or broken TEI is NOT an outage. The rows are already ordered by the
    blend that shipped before any of this existed, so the fallback is to hand
    them back untouched with a WARNING - the same contract the parser has, where
    a failure degrades to plain semantic search. The elapsed time is still
    reported, because time spent failing is still time the user waited.
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

    threshold defaults to settings.review_threshold. Passing it explicitly is
    how Weekend 3 sweeps values without editing a query.
    """
    # A "popular" request raises the review floor; max() rather than override,
    # so it can never drop below the baseline quality gate that keeps
    # 10-review shovelware out of every result set.
    base_threshold = settings.review_threshold if threshold is None else threshold

    # Widen the filters BEFORE anything expensive, if they cannot fill a page.
    # Runs on capped counts (~11-24ms each), never on retried searches, so a
    # relaxed query still pays exactly one embed and one rerank. `parsed` is
    # rebound deliberately: from here down it means what was actually applied,
    # which is what the response reports and what the chips must show.
    should_relax = settings.relax_filters if relax_filters is None else relax_filters
    relaxed_steps: list[RelaxationStep] = []
    relax_ms: float | None = None
    if should_relax:
        # Timed like every other stage. Its cost is a claim this project has
        # made in writing - one capped count per rung, 11-24ms - and a claim
        # that cannot be checked in production is an assertion.
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
        # ~1KB x 200 rows per search, which is why no other mode selects it. The
        # arctic document prefix lives in embed_texts() and was never stored, so
        # this needs no stripping before it reaches a different model.
        pool_columns.append(Game.embed_text.label("embed_text"))

    candidates = select(*pool_columns).where(
        and_(
            Game.embedding.isnot(None),
            Game.total_reviews > effective_threshold,
        )
    )
    candidates = _apply_filters(candidates, parsed)

    # Raw distance, not the derived score: only this form uses the index. The
    # operator must stay cosine to match the index's vector_cosine_ops. Stage 2
    # reranks what comes back, so the blend never touches this ORDER BY.
    cand = (
        candidates.order_by(distance).limit(settings.rerank_candidates).subquery("cand")
    )

    rank_score = _rank_score(cand)
    # "none" orders by the raw distance rather than by the equivalent
    # `1 - dist` descending, so the no-op default is the same expression the
    # single-stage query used and not merely an equal one.
    ordering = cand.c.dist if settings.rank_method == "none" else rank_score.desc()

    stmt = select(
        *cand.c, (1 - cand.c.dist).label("score"), rank_score.label("rank_score")
    ).order_by(ordering)
    # The cross-encoder needs the WHOLE pool. Reordering the ten rows the old
    # ranking already picked could not surface anything it had buried, which is
    # the entire point - 9 of 9 tail misses sit between rank 11 and 200.
    if not reranking:
        stmt = stmt.limit(limit)

    query_start = time.perf_counter()
    with session_scope() as session:
        # HNSW gathers a fixed candidate pool and filters afterwards, so a
        # selective WHERE silently returns fewer rows than asked for - measured
        # 4 of 10 at threshold 5000. Iterative scan re-scans until LIMIT is
        # satisfied. SET LOCAL keeps it inside this transaction rather than
        # leaking onto a pooled connection.
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        # pgvector's default of 40 is smaller than the pool stage 2 reranks,
        # and on its own it cost 3.3 recall points against an exact scan
        # (15.0% vs 18.3% at threshold 10) for 8ms of latency. Interpolated
        # rather than bound: SET takes no parameters. int() is the guard.
        session.execute(
            text(f"SET LOCAL hnsw.ef_search = {int(settings.hnsw_ef_search)}")
        )
        rows = session.execute(stmt).all()
    query_ms = (time.perf_counter() - query_start) * 1000

    # Stage 2 already ran in SQL; in rerank mode stage 3 overrides its ordering
    # and its scores. Pairing each row with its score here rather than reading
    # row.rank_score below is what lets the two paths share one result builder.
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
