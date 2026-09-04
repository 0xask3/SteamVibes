"""Recognise a game the query names, and borrow its tags.

"extremely hard to beat game like elden ring" is the shape this exists for.
The embedding cannot help: "elden ring" sits 133rd of 452 tags away from
`Souls-like` (failures.md #20). But the ELDEN RING row already carries
`Souls-like`, `Difficult` and `Dark Fantasy` - the answer is in the corpus, so
look it up rather than infer it.

No LLM involved. One indexed-ish scan, measured at 1-2ms.
"""

import logging
import re

from sqlalchemy import select, text

from app.config import settings
from app.db import session_scope
from app.models import Game
from app.schemas import ParsedQuery

logger = logging.getLogger(__name__)

# Tags appended to the semantic query from a matched game. Six is roughly what
# a Steam page shows above the fold, and enough to characterise a game without
# swamping the user's own words in the embedded text.
TAGS_FROM_REFERENCE = 6

# Below this a name is too generic to be a deliberate reference. "Beat", "GAME"
# and "Doll" are all real titles.
MIN_NAME_LENGTH = 6

# Detected in code rather than added to the parser prompt: that prompt is
# saturated, and three separate edits to it have now silently destroyed a
# working filter (failures.md #13, #22). A missed phrasing costs one unwanted
# result; a broken prompt costs a filter across every query.
_EXCLUDERS = r"(?:not|non|no|except|excepting|excluding|exclude|without|minus|besides)"

# "not including itself", "but not that one" - the game is referred to, not named.
_EXCLUDE_SELF = re.compile(
    rf"\b{_EXCLUDERS}\b[^.?!]{{0,20}}\b"
    r"(?:itself|that one|this one|the original|it)\b",
    re.IGNORECASE,
)


def wants_reference_excluded(query: str, matched_phrase: str | None = None) -> bool:
    """True when the query says to leave out the game it named.

    Two phrasings, and both occur. "not including itself" refers to the game
    obliquely; "excluding call of duty" names it again. The second needs the
    phrase that actually matched, because only then do we know what to look
    for an excluder in front of.
    """
    if _EXCLUDE_SELF.search(query):
        return True

    if matched_phrase:
        # An excluder within a few words before the title: "suggest some
        # excluding call of duty". Bounded so "not scary ... like call of duty"
        # does not read as excluding it.
        near = re.compile(
            rf"\b{_EXCLUDERS}\b(?:\W+\w+){{0,2}}\W+{re.escape(matched_phrase)}",
            re.IGNORECASE,
        )
        if near.search(query):
            return True

    return False


def apply_reference(parsed: ParsedQuery, query: str) -> ParsedQuery:
    """Fold a named game's tags into `parsed`, mutating and returning it.

    Never raises and never narrows the result set on its own: the tags are
    appended to semantic_query rather than added to required_tags. Requiring
    all six of ELDEN RING's tags would return almost nothing, and picking a
    subset would be arbitrary - biasing the query vector has no such cliff.
    """
    match = _find_referenced_game(query)
    if match is None:
        return parsed

    name, app_id, tags, phrase = match
    parsed.reference_game = name

    borrowed = [tag for tag in tags[:TAGS_FROM_REFERENCE] if tag]
    if borrowed:
        joined = ", ".join(borrowed)
        parsed.semantic_query = f"{parsed.semantic_query.rstrip('. ')}. {joined}"
        logger.info("query references %r, borrowed tags: %s", name, joined)

    # Only when asked. "like elden ring" without a negation should still be
    # allowed to return Elden Ring - it is the best match for itself.
    if wants_reference_excluded(query, phrase):
        # The whole franchise, not the single entry. "excluding call of duty"
        # means all of it; excluding only `Call of Duty®` left Modern Warfare
        # and Black Ops Cold War in the results, which is not what was asked.
        # The match was a prefix, so the exclusion is one too.
        parsed.excluded_app_ids = _franchise_app_ids(phrase) or [app_id]
        logger.info(
            "excluding %d title(s) matching %r", len(parsed.excluded_app_ids), phrase
        )

    return parsed


# Enough for any real franchise - Call of Duty has ~30 entries on Steam - while
# bounding the NOT IN if a short phrase somehow matches half the catalogue.
MAX_FRANCHISE_EXCLUSIONS = 100


def _franchise_app_ids(phrase: str | None) -> list[int]:
    """Every game whose name starts with `phrase`. Empty when phrase is None."""
    if not phrase:
        return []

    stmt = (
        select(Game.app_id)
        .where(text("lower(games.name) LIKE :pattern"))
        .limit(MAX_FRANCHISE_EXCLUSIONS)
    )
    with session_scope() as session:
        return list(session.scalars(stmt, {"pattern": f"{phrase.lower()}%"}).all())


def _word_ngrams(query: str, longest: int = 5, shortest: int = 2) -> list[str]:
    """Word windows from the query, longest first.

    "like call of duty, but" yields "like call of duty", "call of duty, but",
    ... down to two-word pairs. One of them is the game's name, or its opening.
    """
    words = re.findall(r"[\w'®™:-]+", query)
    return [
        " ".join(words[i : i + size])
        for size in range(longest, shortest - 1, -1)
        for i in range(len(words) - size + 1)
    ]


def _find_referenced_game(query: str) -> tuple[str, int, list[str], str | None] | None:
    """The best-known game whose name starts with a phrase from the query.

    Prefix, not substring. Steam stores franchises with trademark and edition
    suffixes - `Call of Duty®`, `DARK SOULS™: Prepare To Die Edition` - so the
    name is *longer* than what anyone types. Asking whether the name appears
    inside the query fails for exactly the games most likely to be referenced;
    asking whether a phrase from the query opens the name succeeds, and needs
    no pg_trgm extension or migration to do it.

    The review floor is what makes it safe. Without it "nothing scary" matches
    a game called `Nothing` and "under 20 dollars" matches `Dollar`, both real
    titles. See settings.title_match_min_reviews.

    Most-reviewed wins, so "dark souls" resolves to DARK SOULS III rather than
    an obscure entry in the same franchise.
    """
    candidates = [gram for gram in _word_ngrams(query) if len(gram) >= MIN_NAME_LENGTH]
    if not candidates:
        return None

    stmt = (
        select(Game.name, Game.app_id, Game.tags)
        .where(
            Game.total_reviews >= settings.title_match_min_reviews,
            text("lower(games.name) LIKE ANY(:patterns)"),
        )
        .order_by(Game.total_reviews.desc())
        .limit(1)
    )

    with session_scope() as session:
        row = session.execute(
            stmt, {"patterns": [f"{gram.lower()}%" for gram in candidates]}
        ).first()

    if row is None:
        return None

    # Which phrase actually opened the name. Needed to spot "excluding <title>"
    # - we have to know where the title sits in the query to look in front of
    # it. Longest first, so "call of duty" wins over "call of".
    lowered = row.name.lower()
    phrase = next((g for g in candidates if lowered.startswith(g.lower())), None)

    return row.name, row.app_id, list(row.tags or []), phrase
