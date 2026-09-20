"""Single-game lookup for GET /api/game/{app_id}.

Separate from app/search.py so the ranking path stays undiluted, and so this
does not drag in the embedding client to read one row.
"""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import session_scope
from app.models import Game
from app.schemas import GameDetail


def get_game(app_id: int) -> GameDetail | None:
    """One game, or None when the id does not exist.

    None rather than an exception: "no such game" is an ordinary answer to an
    arbitrary URL, and the caller turns it into a 404.
    """
    stmt = (
        select(Game)
        .where(Game.app_id == app_id)
        # Without these, reading .genres and .categories fires a query each -
        # cheap here at one row, but the pattern is what bites in a loop.
        .options(selectinload(Game.genres), selectinload(Game.categories))
    )

    with session_scope() as session:
        game = session.scalars(stmt).one_or_none()
        if game is None:
            return None

        # Duplicated from app/search.py rather than imported: three lines would
        # pull the whole search path in behind them.
        platforms = [
            name
            for name, supported in (
                ("Windows", game.windows),
                ("Mac", game.mac),
                ("Linux", game.linux),
            )
            if supported
        ]

        return GameDetail(
            app_id=game.app_id,
            name=game.name,
            short_description=game.short_description,
            detailed_description=game.detailed_description,
            release_date=game.release_date,
            developers=list(game.developers or []),
            publishers=list(game.publishers or []),
            list_price_usd=game.list_price_usd,
            discount_pct=game.discount_pct,
            is_free=game.is_free,
            total_reviews=game.total_reviews,
            positive_reviews=game.positive_reviews,
            positive_ratio=(
                game.positive_reviews / game.total_reviews
                if game.total_reviews
                else None
            ),
            metacritic_score=game.metacritic_score,
            estimated_owners=game.estimated_owners,
            required_age=game.required_age,
            platforms=platforms,
            header_image=game.header_image,
            # Already stored votes-first, so no join and no re-sorting.
            tags=list(game.tags or []),
            genres=sorted(row.genre for row in game.genres),
            categories=sorted(row.category for row in game.categories),
        )
