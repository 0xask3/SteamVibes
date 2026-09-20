"""Turn a natural-language query into a ParsedQuery.

Hard constraints become SQL filters; whatever is left is the vibe and goes to
the embedding model. Failures are expected rather than exceptional: anything
that goes wrong degrades to pure semantic search on the raw text, with a log
line. Never a bare except, never silence.
"""

import difflib
import logging
import re
import time
from functools import lru_cache
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.llm import chat_json
from app.metrics import note
from app.models import GameTag
from app.schemas import ParsedQuery
from app.title_lookup import apply_reference

logger = logging.getLogger(__name__)

# Above this, difflib's ratio is a confident match ("Base Building" ->
# "Base-Building" scores ~0.96). Below it, drop the tag rather than guess: a
# wrong tag returns zero rows with no error.
FUZZY_CUTOFF = 0.85

# The layout is tuned and the two blocks compete - whichever sits nearest the
# query wins. Vocabulary last keeps tag extraction; vocabulary first collapses
# it. Never edit this without re-running eval/compare_parsers.py.
SYSTEM_PROMPT = """\
You convert a player's description of a game into a search filter.

Extract every hard constraint into its own field, then write semantic_query as
the original request with those constraints removed - only the mood, genre feel
or subject matter left behind.

Rules:
- Never INFER a constraint the query does not state. An unmentioned field stays
  null. This applies to price, platform, year, age and multiplayer - guessing
  one of those silently returns the wrong games.
- max_price_usd / min_price_usd: only when the query gives a NUMBER, or says
  "free" / "kostenlos" (which means max_price_usd = 0). Vague words like
  "cheap", "affordable", "budget" or "günstig" are NOT prices. Leave the
  price null and let the word stay in semantic_query.
- multiplayer: true only when the query mentions playing WITH other people -
  co-op, versus, multiplayer, "with a friend", "für zwei". false only when it
  explicitly asks to play alone - singleplayer, solo, "allein". Otherwise
  null. Mood says NOTHING about player count: "cozy", "relaxing",
  "entspannt" and "gemütlich" must never set this field.
- platforms: only when an operating system is named - "runs on linux",
  "for mac", "on windows".
- released_after: a four-digit year, only when a time period is mentioned.
- max_required_age: only when an age is stated. "for a 7 year old" means 7.
- The query may be English or German. Extract filters either way, and keep
  semantic_query in the language it was written in.

Tags are the opposite case: they are how the search actually finds games, so
required_tags is usually NOT empty. Almost every genre, activity or mood word
already exists as a tag - scan the list before deciding nothing fits.
  "base builder" -> Base-Building        "co-op" -> Co-op
  "farming" -> Farming                   "fishing" -> Fishing
  "roguelike" / "changes every run" -> Rogue-lite
  "like dark souls" -> Souls-like        "entspannt" -> Relaxing
  "gemütlich" / "cozy" -> Cozy           "rundenbasiert" -> Turn-Based Strategy
A negation - "not horror", "nothing scary", "keine Gewalt" - goes in
excluded_tags instead. Only a tag that appears nowhere in the list below may be
left out; never invent one, and never reword one.

Worked example.
  Query: co-op base builder under 20 dollars that runs on linux
    max_price_usd: 20
    platforms: ["linux"]
    multiplayer: true
    required_tags: ["Co-op", "Base-Building"]
    semantic_query: "base builder"
  Every constraint moved into a field, semantic_query keeps only the vibe, and
  the tags are copied character for character from the list.

These are the only tags that exist. Copy them EXACTLY, including hyphens and
capitals:
{tags}
"""


# Detected here rather than in the prompt: the prompt is full, and adding this
# to it destroyed a working filter (failures.md #22). German adjectives inflect,
# so `beliebt\w*` - bare `beliebt` matches nothing anyone writes.
_POPULAR = re.compile(
    r"\b(?:popular|well[-\s]known|famous|best[-\s]?selling|beliebt\w*|bekannt\w*)\b",
    re.IGNORECASE,
)

# Punctuation stranded by removing a word mid-sentence.
_DANGLING = re.compile(r"[\s,;.]+$|^[\s,;.]+")


def wants_popular(query: str) -> bool:
    """True when the query asks for widely-played games."""
    return _POPULAR.search(query) is not None


def _strip_popular(query: str) -> str:
    """Remove the popularity words that `min_reviews` has already consumed.

    A constraint converted into a filter must stop steering the vector
    (failures.md #34). Reuses the detecting pattern so trigger and removal
    cannot drift apart, and returns "" when nothing else was in the query -
    the caller decides what to do rather than embedding an empty string.
    """
    cleaned = _POPULAR.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return _DANGLING.sub("", cleaned).strip()


# A code rule is also the net under an intent the prompt already holds:
# `multiplayer` is in the prompt and the model fills it correctly until four
# clauses compete, then returns null. See failures.md #32.
_SINGLEPLAYER = re.compile(
    r"\b(?:single[-\s]?player|solo|singleplayer|einzelspieler|allein\w*)\b"
    r"|\bplay(?:ing)?\s+(?:alone|by\s+myself|on\s+my\s+own)\b"
    r"|\bf(?:ü|ue)r\s+einen\s+spieler\b",
    re.IGNORECASE,
)

# Every such regex needs a negation guard. A missing filter returns too much; a
# backwards one returns confidently wrong results.
_NOT_SINGLEPLAYER = re.compile(
    r"\b(?:not|no|non|without|except|excluding|kein\w*|nicht)\b\W+(?:\w+\W+){0,2}?"
    r"(?:single[-\s]?player|solo|singleplayer|einzelspieler|allein\w*)\b",
    re.IGNORECASE,
)


def wants_singleplayer(query: str) -> bool:
    """True when the query explicitly asks to play alone.

    One-directional on purpose: there is no `wants_multiplayer()`, because "no
    multiplayer" contains "multiplayer" and the model was never observed
    getting that direction wrong.
    """
    if _NOT_SINGLEPLAYER.search(query):
        return False
    return _SINGLEPLAYER.search(query) is not None


# Derived in code, so the model never sees them: it cannot know a real app_id,
# and a hallucinated one silently removes a real result.
_CODE_ONLY_FIELDS = ("reference_game", "excluded_app_ids", "min_reviews")

# Fields the model MUST emit a key for, even if null. An optional property is a
# grammar branch the model may skip, and skipping is indistinguishable from "not
# asked for"; it also restores the field order. The tag ARRAYS stay optional
# deliberately - forcing them makes the model invent a tag for a vague query and
# costs 15.9 points of tail recall. See failures.md #33.
_REQUIRED_FIELDS = (
    "max_price_usd",
    "min_price_usd",
    "platforms",
    "released_after",
    "multiplayer",
    "max_required_age",
    "semantic_query",
)


@lru_cache(maxsize=1)
def _llm_schema() -> dict[str, Any]:
    """ParsedQuery's JSON schema, minus the code-only fields."""
    schema = ParsedQuery.model_json_schema()
    properties = schema.get("properties", {})
    for field in _CODE_ONLY_FIELDS:
        properties.pop(field, None)
    # Intersected rather than assigned: a renamed field would otherwise require
    # a property that does not exist, and Ollama would reject every parse.
    schema["required"] = [f for f in _REQUIRED_FIELDS if f in properties]
    return schema


# How long a NON-EMPTY vocabulary is trusted. The read is 52ms against a
# ~1,200ms parse, so refreshing costs nothing measurable.
VOCABULARY_TTL_S = 300.0

_vocabulary: tuple[str, ...] = ()
_vocabulary_read_at = 0.0


def get_tag_vocabulary() -> tuple[str, ...]:
    """Every real tag. All 452 fit in the prompt in ~1,400 tokens.

    Deliberately not an lru_cache: `docker compose up` starts the API before
    ingest, so a cache would hold the tags of an EMPTY database for the life of
    the process. An empty result is never cached, and a non-empty one expires,
    because a search during `load_games` would otherwise pin a partial list.
    No lock - racing threads run the same idempotent read.
    """
    global _vocabulary, _vocabulary_read_at
    now = time.monotonic()
    if _vocabulary and now - _vocabulary_read_at < VOCABULARY_TTL_S:
        return _vocabulary

    with session_scope() as session:
        tags = tuple(
            session.scalars(select(GameTag.tag).distinct().order_by(GameTag.tag)).all()
        )
    if not tags:
        # Degraded, not broken - but the response looks exactly like a query
        # with no tag intent, so this line is the only visible trace.
        logger.warning(
            "tag vocabulary is empty - no games loaded yet, so the parser "
            "extracts no tags and the explanation check cannot scan prose"
        )
    _vocabulary, _vocabulary_read_at = tags, now
    return tags


def _resolve_tag(candidate: str, vocabulary: tuple[str, ...]) -> str | None:
    """Map a model-produced tag onto a real one, or None to drop it.

    Exact, then case-insensitive, then fuzzy. Dropping beats guessing: a tag
    that does not exist filters every row away and looks like "no results"
    rather than like an error.
    """
    if candidate in vocabulary:
        return candidate

    lowered = {tag.casefold(): tag for tag in vocabulary}
    if candidate.casefold() in lowered:
        real = lowered[candidate.casefold()]
        logger.info("tag %r matched %r by case", candidate, real)
        return real

    close = difflib.get_close_matches(candidate, vocabulary, n=1, cutoff=FUZZY_CUTOFF)
    if close:
        logger.info("tag %r fuzzy-matched to %r", candidate, close[0])
        return close[0]

    logger.warning("dropping unknown tag %r - no match above %.2f", candidate, FUZZY_CUTOFF)
    return None


def _resolve_tags(candidates: list[str], vocabulary: tuple[str, ...]) -> list[str]:
    resolved = [_resolve_tag(tag, vocabulary) for tag in candidates]
    return [tag for tag in resolved if tag is not None]


# Making a field required can make the model invent a value for it: asked for no
# OS it sometimes fills all three, and platforms are ANDed. Only the three real
# `Platform` values, so the guard cannot fire on a query that named one.
_OS_NAMED = re.compile(r"\b(?:windows|linux|mac(?:os)?|osx)\b", re.IGNORECASE)


def _drop_invented_platforms(parsed: ParsedQuery, text: str) -> None:
    if len(parsed.platforms) == 3 and not _OS_NAMED.search(text):
        logger.info("dropping invented platforms %s - query names no OS", parsed.platforms)
        parsed.platforms = []


def parse_query(text: str, model: str | None = None) -> ParsedQuery:
    """Natural language in, ParsedQuery out.

    Never raises. On any failure it returns a ParsedQuery carrying the raw text
    and no filters - the pre-parser behaviour, which is known to work.
    """
    vocabulary = get_tag_vocabulary()

    try:
        raw = chat_json(
            system=SYSTEM_PROMPT.format(tags=", ".join(vocabulary)),
            user=text,
            schema=_llm_schema(),
            model=model,
        )
    except Exception:
        # Transport failure, missing model, non-JSON content. The code rules
        # below still apply, so "like elden ring" works with no chat model.
        logger.warning("parser call failed for %r, falling back", text, exc_info=True)
        # Counted because a bare ParsedQuery is indistinguishable downstream
        # from a query that carried no constraints. Split from the bad-output
        # case below: unreachable Ollama and an unusable answer need different
        # fixes.
        note("parse_call_failed")
        return _apply_code_rules(ParsedQuery(semantic_query=text), text)

    try:
        parsed = ParsedQuery.model_validate(raw)
    except Exception:
        # Log the raw output, not just the exception - that is what makes a
        # schema-valid but semantically wrong answer debuggable.
        logger.warning(
            "parser returned unusable output for %r: %r", text, raw, exc_info=True
        )
        note("parse_bad_output")
        return _apply_code_rules(ParsedQuery(semantic_query=text), text)

    # The model was told to copy tags exactly. It will not always.
    parsed.required_tags = _resolve_tags(parsed.required_tags, vocabulary)
    parsed.excluded_tags = _resolve_tags(parsed.excluded_tags, vocabulary)

    # Before the chips are built, so the UI never shows three platform chips the
    # user did not ask for.
    _drop_invented_platforms(parsed, text)

    # An empty semantic_query would embed nothing useful. Prefer the raw text.
    if not parsed.semantic_query.strip():
        logger.info("parser emptied semantic_query for %r, using raw text", text)
        parsed.semantic_query = text

    # Last, because it appends to whatever semantic_query ended up being.
    return _apply_code_rules(parsed, text)


def _apply_code_rules(parsed: ParsedQuery, text: str) -> ParsedQuery:
    """Intents read from the query text rather than from the model.

    Everything here either broke the prompt when added to it or is something a
    model cannot know. Applied on the fallback paths too.
    """
    if parsed.min_reviews is None and wants_popular(text):
        parsed.min_reviews = settings.popular_min_reviews
        logger.info("query asks for popular, min_reviews=%d", parsed.min_reviews)
        # Stripped in the same branch that consumed the intent, so filter and
        # text cannot disagree. Guarded: parse_query's empty check runs before
        # this, so a query of literally "popular" would embed "".
        stripped = _strip_popular(parsed.semantic_query)
        if stripped:
            parsed.semantic_query = stripped
        else:
            logger.info("not stripping %r - nothing would be left", parsed.semantic_query)

    # Fills, never overrides: the model was only observed returning no value
    # here, and an override would break "single player or co-op".
    if parsed.multiplayer is None and wants_singleplayer(text):
        parsed.multiplayer = False
        logger.info("query asks to play alone, multiplayer=False")

    # Last on purpose: it reads parsed.multiplayer and parsed.excluded_tags to
    # decide which of the referenced game's tags it may borrow.
    return apply_reference(parsed, text)
