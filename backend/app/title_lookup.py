"""Recognise a game the query names, and borrow its tags.

The embedding cannot: "elden ring" sits 133rd of 452 tags away from
`Souls-like` (failures.md #20), while the ELDEN RING row already carries it.
No LLM involved - one scan, 1-2ms.
"""

import logging
import re

from sqlalchemy import select, text

from app.config import settings
from app.db import session_scope
from app.models import Game
from app.schemas import ParsedQuery

logger = logging.getLogger(__name__)

# Enough to characterise a game without swamping the user's own words.
TAGS_FROM_REFERENCE = 6

# Tags that contradict `multiplayer=False`. Read off the real vocabulary, not
# guessed - a tag that does not exist would sit here looking like it worked.
# `PvE`, `Team-Based` and `Social Deduction` are absent on purpose: a
# singleplayer game can legitimately carry all three.
MULTIPLAYER_TAGS = frozenset(
    {
        "4 Player Local",
        "Asynchronous Multiplayer",
        "Co-op",
        "Co-op Campaign",
        "Local Co-Op",
        "Local Multiplayer",
        "MMORPG",
        "Massively Multiplayer",
        "Multiplayer",
        "Online Co-Op",
        "PvP",
        "Split Screen",
    }
)

# 5, not 6, or `Hades`, `Stray` and `Forza` cannot be reached at all. The real
# guard is TITLE_MATCH_MIN_REVIEWS. Re-measure the 4 false positives over the
# 118 eval queries before loosening this.
MIN_NAME_LENGTH = 5

# In code rather than in the parser prompt: that prompt is full, and edits to it
# have silently destroyed a working filter (failures.md #13, #22).
_EXCLUDERS = r"(?:not|non|no|except|excepting|excluding|exclude|without|minus|besides)"

# A ONE-WORD title is only a candidate when a reference cue precedes it.
# `_word_ngrams` makes 2-to-5 word windows, so single-word names were
# unreachable; generating every single word instead took false positives from 4
# to 24 ("first person puzzle" matched `Persona 5 Royal`). See failures.md #35.
_REFERENCE_CUE = re.compile(
    r"\b(?:like|similar\s+to|such\s+as|reminiscent\s+of|comparable\s+to"
    r"|excluding|exclude|except|without|besides|minus"
    r"|wie|ähnlich\s+wie|außer|ausser|ohne)\s+([\w'®™:-]+)",
    re.IGNORECASE,
)

# "not including itself", "but not that one" - the game is referred to, not named.
_EXCLUDE_SELF = re.compile(
    rf"\b{_EXCLUDERS}\b[^.?!]{{0,20}}\b"
    r"(?:itself|that one|this one|the original|it)\b",
    re.IGNORECASE,
)


def wants_reference_excluded(query: str, matched_phrase: str | None = None) -> bool:
    """True when the query says to leave out the game it named.

    "not including itself" refers to it obliquely; "excluding call of duty"
    names it again, which needs the phrase that actually matched.
    """
    if _EXCLUDE_SELF.search(query):
        return True

    if matched_phrase:
        # Bounded, so "not scary ... like call of duty" does not read as
        # excluding it.
        near = re.compile(
            rf"\b{_EXCLUDERS}\b(?:\W+\w+){{0,2}}\W+{re.escape(matched_phrase)}",
            re.IGNORECASE,
        )
        if near.search(query):
            return True

    return False


def _contradicts_filters(tag: str, parsed: ParsedQuery) -> bool:
    """True when borrowing `tag` would fight a filter the query already set.

    The game's tags describe the GAME, not the request: "like resident evil but
    nothing scary" excluded Horror in SQL while appending it to the embedded
    text. Exact match only - a substring rule would make an excluded `Action`
    drop `Action RPG` too. See failures.md #32.
    """
    if tag in parsed.excluded_tags:
        return True
    if parsed.multiplayer is False and tag in MULTIPLAYER_TAGS:
        return True
    return parsed.multiplayer is True and tag == "Singleplayer"


def apply_reference(parsed: ParsedQuery, query: str) -> ParsedQuery:
    """Fold a named game's tags into `parsed`, mutating and returning it.

    Tags are appended to semantic_query, never added to required_tags:
    requiring all six of a game's tags returns almost nothing. Reads
    `excluded_tags` and `multiplayer`, so it must run after the code rules.
    """
    match = _find_referenced_game(query)
    if match is None:
        return parsed

    name, app_id, tags, phrase = match
    parsed.reference_game = name

    wanted = [tag for tag in tags[:TAGS_FROM_REFERENCE] if tag]
    borrowed = [tag for tag in wanted if not _contradicts_filters(tag, parsed)]
    if dropped := [tag for tag in wanted if tag not in borrowed]:
        # Logged because this changes what gets embedded, and a silent change to
        # the query vector produces plausible results forever.
        logger.info("not borrowing %s - contradicts the query", ", ".join(dropped))

    if borrowed:
        joined = ", ".join(borrowed)
        parsed.semantic_query = f"{parsed.semantic_query.rstrip('. ')}. {joined}"
        logger.info("query references %r, borrowed tags: %s", name, joined)

    # Only when asked. "like elden ring" without a negation should still be
    # allowed to return Elden Ring - it is the best match for itself.
    if wants_reference_excluded(query, phrase):
        # The whole franchise: the match was a prefix, so the exclusion is one
        # too - which also drops sequels and spinoffs.
        parsed.excluded_app_ids = _franchise_app_ids(phrase) or [app_id]
        logger.info(
            "excluding %d title(s) matching %r", len(parsed.excluded_app_ids), phrase
        )

    return parsed


# Enough for any real franchise, while bounding the NOT IN if a short phrase
# matches half the catalogue.
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

    One of them is a multi-word game's name, or its opening. Single words come
    from `_REFERENCE_CUE` instead.
    """
    words = re.findall(r"[\w'®™:-]+", query)
    return [
        " ".join(words[i : i + size])
        for size in range(longest, shortest - 1, -1)
        for i in range(len(words) - size + 1)
    ]


def _find_referenced_game(query: str) -> tuple[str, int, list[str], str | None] | None:
    """The best-known game whose name starts with a phrase from the query.

    PREFIX, not substring: Steam names carry trademark and edition suffixes
    (`Call of Duty®`), so they are longer than what anyone types, and asking
    whether the name sits inside the query fails for exactly the games people
    reference. No pg_trgm, no migration - failures.md #23. The review floor is
    what makes it safe, and most-reviewed wins.
    """
    # Cued singles go LAST, after the longest-first windows, so "excluding call
    # of duty" resolves the phrase to `call of duty` and not to `call` - and
    # that phrase is what wants_reference_excluded() reads.
    cued = [m.group(1) for m in _REFERENCE_CUE.finditer(query)]
    candidates = [
        gram
        for gram in _word_ngrams(query) + cued
        if len(gram) >= MIN_NAME_LENGTH
    ]
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

    # Which phrase actually opened the name, so "excluding <title>" knows where
    # to look. Longest first, so "call of duty" wins over "call of".
    lowered = row.name.lower()
    phrase = next((g for g in candidates if lowered.startswith(g.lower())), None)

    return row.name, row.app_id, list(row.tags or []), phrase
