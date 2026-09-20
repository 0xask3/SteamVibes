"""Widen the filters when they starve the index, one constraint at a time.

Stack a price, a year and a tag list and the WHERE clause can leave the vector
nothing to rank. **No model is in the loop**: an agent would ask the LLM which
constraint to drop, this asks a table, which is testable, reproducible, free,
and cannot invent a constraint that was never there.

Two choices carry the whole thing:

  IT RUNS ON COUNTS, NOT RETRIED SEARCHES. Re-running search() costs ~1.1s of
  cross-encoder per attempt; a capped count is 11-24ms, so the ladder is walked
  first and exactly one real search runs at the end.

  SOME CONSTRAINTS ARE NEVER RELAXED. A short page is a disappointment; a
  confidently wrong page is a defect. See the list above LADDER.
"""

import logging
from collections.abc import Callable

from sqlalchemy import func, select

from app.db import session_scope
from app.models import Game
from app.schemas import ParsedQuery, RelaxationStep

logger = logging.getLogger(__name__)

# Two doublings turns "under $10" into "under $40", still recognisably the same
# request; a third would not be.
MAX_PRICE_DOUBLINGS = 2

# Notes use an ASCII hyphen, never an em dash: the CLI prints these too, and a
# Windows console renders U+2014 as a replacement character.


def _drop_tags(parsed: ParsedQuery) -> tuple[ParsedQuery, RelaxationStep] | None:
    if not parsed.required_tags:
        return None
    was = ", ".join(parsed.required_tags)
    return parsed.model_copy(update={"required_tags": []}), RelaxationStep(
        field="required_tags",
        was=was,
        now="(any)",
        note=f"Nothing tagged {was} matched - showing results without that filter.",
    )


def _drop_min_reviews(parsed: ParsedQuery) -> tuple[ParsedQuery, RelaxationStep] | None:
    if parsed.min_reviews is None:
        return None
    return parsed.model_copy(update={"min_reviews": None}), RelaxationStep(
        field="min_reviews",
        was=f"{parsed.min_reviews:,}+ reviews",
        now="(any)",
        note=(
            f"Too few games with {parsed.min_reviews:,}+ reviews - "
            "including less popular ones."
        ),
    )


def _drop_year(parsed: ParsedQuery) -> tuple[ParsedQuery, RelaxationStep] | None:
    if parsed.released_after is None:
        return None
    return parsed.model_copy(update={"released_after": None}), RelaxationStep(
        field="released_after",
        was=f"after {parsed.released_after}",
        now="(any year)",
        note=f"Too few released after {parsed.released_after} - including older games.",
    )


def _drop_min_price(parsed: ParsedQuery) -> tuple[ParsedQuery, RelaxationStep] | None:
    if parsed.min_price_usd is None:
        return None
    return parsed.model_copy(update={"min_price_usd": None}), RelaxationStep(
        field="min_price_usd",
        was=f"over ${parsed.min_price_usd:.2f}",
        now="(any price)",
        note=f"Too few over ${parsed.min_price_usd:.2f} - dropping the minimum price.",
    )


def _widen_max_price(parsed: ParsedQuery) -> tuple[ParsedQuery, RelaxationStep] | None:
    if parsed.max_price_usd is None:
        return None
    # Rounded to cents so the note reads like a price, not like arithmetic.
    was = parsed.max_price_usd
    now = round(was * 2, 2)
    return parsed.model_copy(update={"max_price_usd": now}), RelaxationStep(
        field="max_price_usd",
        was=f"under ${was:.2f}",
        now=f"under ${now:.2f}",
        note=f"No results under ${was:.2f} - showing results under ${now:.2f}.",
    )


# The ladder, least harmful first. The order is a JUDGEMENT, not a measurement:
# there is no eval for "was that the right thing to give up".
#
#   required_tags   a coarse recall gate the vector does better; the intent
#                   survives whole in semantic_query, so this costs the least.
#   min_reviews     "popular" is a casual preference.
#   released_after  loses recency; the vector still ranks what remains.
#   min_price_usd   almost never set, and nobody insists on paying more.
#   max_price_usd   doubled rather than dropped, which keeps half the intent.
#
# NEVER relaxed, and this list is the important half of the file:
#   max_required_age   a safety constraint.
#   excluded_tags      dropping it shows horror to someone who said none.
#   excluded_app_ids   returns the game they excluded by name.
#   multiplayer        returns the wrong KIND of game (failures.md #32).
#   platforms          a compatibility fact, not a preference.
Rung = Callable[[ParsedQuery], "tuple[ParsedQuery, RelaxationStep] | None"]

LADDER: tuple[Rung, ...] = (
    _drop_tags,
    _drop_min_reviews,
    _drop_year,
    _drop_min_price,
    *([_widen_max_price] * MAX_PRICE_DOUBLINGS),
)


def _passing_rows(parsed: ParsedQuery, threshold: int, cap: int) -> int:
    """How many rows clear the filters, counted no further than `cap`.

    Capped because the question is "are there enough", never "how many":
    uncapped is 611ms against 24ms. Reuses _apply_filters (imported lazily, to
    break the cycle) rather than restating the WHERE clauses - a count that
    disagreed with the real query would relax the wrong thing, silently.
    """
    from app.search import _apply_filters

    effective = max(threshold, parsed.min_reviews or 0)
    inner = select(Game.app_id).where(
        Game.embedding.isnot(None), Game.total_reviews > effective
    )
    inner = _apply_filters(inner, parsed).limit(cap)
    with session_scope() as session:
        return int(
            session.scalar(select(func.count()).select_from(inner.subquery())) or 0
        )


def relax(
    parsed: ParsedQuery, limit: int, threshold: int, target: int | None = None
) -> tuple[ParsedQuery, list[RelaxationStep]]:
    """Widen filters until `target` rows survive, or the ladder runs out.

    Returns the filters to search with and the steps taken; an empty list is
    the normal case and costs one count. Re-counts after each rung, so a query
    needing one widening does not lose four filters.
    """
    want = target if target is not None else limit
    steps: list[RelaxationStep] = []

    if _passing_rows(parsed, threshold, want) >= want:
        return parsed, steps

    for rung in LADDER:
        applied = rung(parsed)
        if applied is None:
            continue  # that constraint was not set; try the next rung
        parsed, step = applied
        steps.append(step)
        if _passing_rows(parsed, threshold, want) >= want:
            break

    if steps:
        # Load-bearing: relaxation changes what a search returns, and a silent
        # change to results is what this project refuses to ship.
        logger.info(
            "relaxed %d filter(s) to fill %d results: %s",
            len(steps),
            want,
            "; ".join(s.field for s in steps),
        )
    return parsed, steps
