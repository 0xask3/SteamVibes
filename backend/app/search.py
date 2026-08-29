"""Hybrid search: structured filters in SQL, vibe ranking in pgvector.

The filters come from a ParsedQuery, built either by hand from CLI flags or by
app/query_parser.py from natural language. One code path either way.

Read the query construction carefully - this is the file where a mistake
produces plausible results forever rather than an error.
"""

import time

from sqlalchemy import Select, and_, exists, func, not_, select, text

from app.config import settings
from app.db import session_scope
from app.embedding import embed_query
from app.models import Game, GameCategory, GameTag
from app.schemas import ParsedQuery, SearchResponse, SearchResult

TOP_TAGS_SHOWN = 5

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
        # @> against the GIN index: the row's tags must contain all of these.
        stmt = stmt.where(Game.tags.contains(parsed.required_tags))
    if parsed.excluded_tags:
        # && is "overlaps"; negated, "shares none of these". Games with no tags
        # have an empty array rather than NULL, so they correctly pass.
        stmt = stmt.where(not_(Game.tags.overlap(parsed.excluded_tags)))

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


def search(
    parsed: ParsedQuery, limit: int = 10, threshold: int | None = None
) -> SearchResponse:
    """Filter in SQL, then rank what survives by embedding distance.

    threshold defaults to settings.review_threshold. Passing it explicitly is
    how Weekend 3 sweeps values without editing a query.
    """
    effective_threshold = settings.review_threshold if threshold is None else threshold
    unknown = _unknown_tags(parsed.required_tags + parsed.excluded_tags)

    embed_start = time.perf_counter()
    query_vec = embed_query(parsed.semantic_query)
    embed_ms = (time.perf_counter() - embed_start) * 1000

    distance = Game.embedding.cosine_distance(query_vec)

    stmt = select(
        Game.app_id,
        Game.name,
        Game.short_description,
        (1 - distance).label("score"),
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
    stmt = _apply_filters(stmt, parsed)

    # Raw distance, not the derived score: only this form uses the index. The
    # operator must stay cosine to match the index's vector_cosine_ops.
    stmt = stmt.order_by(distance).limit(limit)

    query_start = time.perf_counter()
    with session_scope() as session:
        # HNSW gathers a fixed candidate pool and filters afterwards, so a
        # selective WHERE silently returns fewer rows than asked for - measured
        # 4 of 10 at threshold 5000. Iterative scan re-scans until LIMIT is
        # satisfied. SET LOCAL keeps it inside this transaction rather than
        # leaking onto a pooled connection.
        session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        rows = session.execute(stmt).all()
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
