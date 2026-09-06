"""Hybrid search: structured filters in SQL, vibe ranking in pgvector.

The filters come from a ParsedQuery, built either by hand from CLI flags or by
app/query_parser.py from natural language. One code path either way.

Read the query construction carefully - this is the file where a mistake
produces plausible results forever rather than an error.
"""

import time

from sqlalchemy import Float, Select, and_, cast, exists, func, not_, select, text
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from app.config import settings
from app.db import session_scope
from app.embedding import embed_query
from app.models import Game, GameCategory, GameTag
from app.schemas import ParsedQuery, SearchResponse, SearchResult

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
    k = settings.rrf_k
    rank_by_similarity = func.row_number().over(order_by=cand.c.dist)
    rank_by_reviews = func.row_number().over(order_by=cand.c.total_reviews.desc())
    return cast(1.0 / (k + rank_by_similarity), Float) + cast(
        weight / (k + rank_by_reviews), Float
    )


def search(
    parsed: ParsedQuery, limit: int = 10, threshold: int | None = None
) -> SearchResponse:
    """Filter in SQL, then rank what survives by embedding distance.

    threshold defaults to settings.review_threshold. Passing it explicitly is
    how Weekend 3 sweeps values without editing a query.
    """
    # A "popular" request raises the review floor; max() rather than override,
    # so it can never drop below the baseline quality gate that keeps
    # 10-review shovelware out of every result set.
    base_threshold = settings.review_threshold if threshold is None else threshold
    effective_threshold = max(base_threshold, parsed.min_reviews or 0)
    unknown = _unknown_tags(parsed.required_tags + parsed.excluded_tags)

    embed_start = time.perf_counter()
    query_vec = embed_query(parsed.semantic_query)
    embed_ms = (time.perf_counter() - embed_start) * 1000

    distance = Game.embedding.cosine_distance(query_vec)

    candidates = select(
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
    ).where(
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

    stmt = (
        select(*cand.c, (1 - cand.c.dist).label("score"), rank_score.label("rank_score"))
        .order_by(ordering)
        .limit(limit)
    )

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

    results = [
        SearchResult(
            app_id=row.app_id,
            name=row.name,
            short_description=row.short_description,
            score=float(row.score),
            rank_score=float(row.rank_score),
            list_price_usd=row.list_price_usd,
            is_free=row.is_free,
            total_reviews=row.total_reviews,
            positive_ratio=(
                row.positive_reviews / row.total_reviews if row.total_reviews else None
            ),
            tags=list(row.top_tags or []),
            platforms=_platforms(row.windows, row.mac, row.linux),
        )
        for row in rows
    ]

    return SearchResponse(
        parsed=parsed,
        results=results,
        unknown_tags=unknown,
        requested=limit,
        returned=len(results),
        threshold=effective_threshold,
        embed_ms=embed_ms,
        query_ms=query_ms,
    )
