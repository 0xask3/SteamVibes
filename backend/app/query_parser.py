"""Turn a natural-language query into a ParsedQuery.

The hard constraints come out as SQL filters; whatever is left is the vibe and
goes to the embedding model.

Per CLAUDE.md, failures here are expected rather than exceptional. Anything
that goes wrong - the model is missing, the JSON is nonsense, a field is the
wrong type - degrades to pure semantic search on the raw text, with a log line.
Never a bare except, never silence.
"""

import difflib
import logging
import re
from functools import lru_cache
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.llm import chat_json
from app.models import GameTag
from app.schemas import ParsedQuery
from app.title_lookup import apply_reference

logger = logging.getLogger(__name__)

# Above this, difflib's ratio is a confident match: "Base Building" ->
# "Base-Building" scores ~0.96. Below it we drop the tag rather than guess,
# because a wrong tag returns zero rows with no error.
FUZZY_CUTOFF = 0.85

# Layout is tuned, not arbitrary. The ~1,400-token tag list goes LAST, directly
# above the query: when it sat at the top instead, tag extraction collapsed -
# query 1 lost both Co-op and Base-Building despite the worked example naming
# them. The scalar rules sit above it and hold their own on wording alone.
#
# The two pulls are in tension. Whichever block sits nearest the query wins, so
# any future edit here needs eval/compare_parsers.py re-run, not just a read.
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


# "Popular" is detected here rather than in the prompt. Tried as a prompt rule
# in the scalar block - on the theory that the two earlier regressions came
# from editing the tag block - and it cost `not War` on one query and
# `multiplayer` on another. Third confirmation that the prompt is full; see
# failures.md #22. Numbers in the query ("at least 500 reviews") are NOT
# handled as a result, which is the price of not touching the prompt.
_POPULAR = re.compile(
    r"\b(?:popular|well[-\s]known|famous|best[-\s]?selling|beliebt|bekannt\w*)\b",
    re.IGNORECASE,
)


def wants_popular(query: str) -> bool:
    """True when the query asks for widely-played games."""
    return _POPULAR.search(query) is not None


# Same reasoning as _POPULAR, from a different direction: `multiplayer` IS in
# the prompt and the model usually fills it, but it falls off the end of a long
# query. Measured at temperature 0 - "call of duty like game, single player",
# "... under 20 dollars, single player" and "... also popular, single player"
# all give False, while "call of duty like game, but not including itself, also
# popular, single player" gives None. Move "single player" earlier in that same
# sentence and it comes back. Four competing clauses is the trigger, not any
# one of them. See failures.md #32.
#
# The prompt is NOT the place to fix that. It is full, and three separate edits
# have each silently destroyed a working filter (failures.md #13, #22).
_SINGLEPLAYER = re.compile(
    r"\b(?:single[-\s]?player|solo|singleplayer|einzelspieler|allein\w*)\b"
    r"|\bplay(?:ing)?\s+(?:alone|by\s+myself|on\s+my\s+own)\b"
    r"|\bf(?:ü|ue)r\s+einen\s+spieler\b",
    re.IGNORECASE,
)

# "not single player", "kein Einzelspieler", "no solo". Without this the regex
# reads a negation as a request and inverts the filter, which is worse than the
# bug it fixes: a missing filter returns too much, a backwards one returns
# confidently wrong results. Mirrors title_lookup._EXCLUDERS.
_NOT_SINGLEPLAYER = re.compile(
    r"\b(?:not|no|non|without|except|excluding|kein\w*|nicht)\b\W+(?:\w+\W+){0,2}?"
    r"(?:single[-\s]?player|solo|singleplayer|einzelspieler|allein\w*)\b",
    re.IGNORECASE,
)


def wants_singleplayer(query: str) -> bool:
    """True when the query explicitly asks to play alone.

    Deliberately one-directional. There is no `wants_multiplayer()`: the model
    handles co-op and versus correctly in every probe, and a `True` regex is the
    riskier half - "no multiplayer" contains "multiplayer", so it would need the
    same negation guard to buy a fix for a failure never observed.
    """
    if _NOT_SINGLEPLAYER.search(query):
        return False
    return _SINGLEPLAYER.search(query) is not None


# Fields the model must never fill. They are derived in code from the
# referenced-game lookup, and the model has no way to know a real app_id -
# left in the schema it invents plausible integers, and a wrong one silently
# removes a real result. Stripping them also saves generation tokens.
_CODE_ONLY_FIELDS = ("reference_game", "excluded_app_ids", "min_reviews")

# Fields the model MUST emit a key for, even if that key is null.
#
# Pydantic marks a field optional whenever it has a default, so every field here
# but semantic_query was optional - and Ollama's `format` compiles an optional
# property into a grammar branch the model may simply skip. It did: asked for
# "...no wars on linux under 30$" it returned max_price_usd ABSENT, not null,
# while correctly stripping "under 30$" out of semantic_query. Absent and "no
# price requested" are the same thing downstream, so the filter vanished with no
# error. Across the compare_parsers set the shipped schema dropped required_tags
# on 64% of parses. See failures.md #33.
#
# Listing them here also restores the field ORDER CLAUDE.md depends on. Optional
# properties let the model emit keys in any order, and it put semantic_query
# FIRST - the exact thing declaring it last was meant to prevent.
#
# required_tags and excluded_tags are deliberately NOT here. Forcing the arrays
# too costs 15.9 points of tail recall (47.7% against 63.6%): a tag the model
# invents for a vague query becomes a filter, and long-tail games are the least
# likely to carry it. They stay optional so the model can decline.
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
    """ParsedQuery's JSON schema, minus the code-only fields.

    None of the code-only fields is in `required` - all three carry defaults -
    so removing the properties leaves a valid schema, and the remaining field
    order is untouched. semantic_query must still come last. See CLAUDE.md.
    """
    schema = ParsedQuery.model_json_schema()
    properties = schema.get("properties", {})
    for field in _CODE_ONLY_FIELDS:
        properties.pop(field, None)
    # Intersected with properties rather than assigned blindly: a field renamed
    # in ParsedQuery would otherwise put a name in `required` that no property
    # satisfies, and Ollama would reject every parse rather than one field.
    schema["required"] = [f for f in _REQUIRED_FIELDS if f in properties]
    return schema


@lru_cache(maxsize=1)
def get_tag_vocabulary() -> tuple[str, ...]:
    """Every real tag. All 452 fit in the prompt in ~1,400 tokens.

    BUILD_PLAN.md assumed a top-200 subset would be needed. Passing all of them
    removes a failure class: a tag that exists but was never shown to the model.
    """
    with session_scope() as session:
        tags = session.scalars(select(GameTag.tag).distinct().order_by(GameTag.tag))
        return tuple(tags.all())


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


# The cost of making `platforms` required: it must now emit the key, and on a
# query naming no OS it sometimes fills all three rather than an empty list -
# "cheap relaxing puzzle games, nothing scary" did it 3 times out of 3. Platforms
# are ANDed in search, so that silently demands a game running on Windows AND
# macOS AND Linux. Measured at 1 of 12 no-OS queries, and 0 of 40 eval queries.
#
# Leaving `platforms` optional instead is worse: the key then goes missing on
# "on linux under 30$" and the real filter disappears, which is the bug this
# whole change exists to fix.
#
# So it is guarded here rather than in the prompt, per the convention in
# CLAUDE.md. The guard only fires when the query names no OS at all, so a genuine
# "runs on windows, mac and linux" survives - and its failure mode is widening
# the results, never narrowing them onto something unasked for.
# Only the three real values of `Platform`. "steam deck" deliberately absent:
# it is not one of them, so listing it would only stop the guard firing on a
# query that still cannot mean "all three".
_OS_NAMED = re.compile(r"\b(?:windows|linux|mac(?:os)?|osx)\b", re.IGNORECASE)


def _drop_invented_platforms(parsed: ParsedQuery, text: str) -> None:
    if len(parsed.platforms) == 3 and not _OS_NAMED.search(text):
        logger.info("dropping invented platforms %s - query names no OS", parsed.platforms)
        parsed.platforms = []


def parse_query(text: str, model: str | None = None) -> ParsedQuery:
    """Natural language in, ParsedQuery out.

    Never raises. On any failure the return value is a ParsedQuery carrying the
    raw text and no filters, which is exactly what search did before the parser
    existed - a degraded path that is already known to work.
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
        # Transport failure, missing model, non-JSON content. Log it with the
        # query so it can be reproduced, then fall back. The referenced-game
        # lookup is pure SQL, so it still applies - "like elden ring" works
        # even with no chat model at all.
        logger.warning("parser call failed for %r, falling back", text, exc_info=True)
        return _apply_code_rules(ParsedQuery(semantic_query=text), text)

    try:
        parsed = ParsedQuery.model_validate(raw)
    except Exception:
        # Schema-valid JSON can still be semantically wrong. Log the raw output
        # rather than just the exception - that is what makes it debuggable.
        logger.warning(
            "parser returned unusable output for %r: %r", text, raw, exc_info=True
        )
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

    Everything here was either measured to break the prompt when added to it,
    or is not something a language model can know - a real app_id, for
    instance. Applied on the fallback paths too, so a query naming a game or
    asking for popular titles still works with no chat model at all.
    """
    if parsed.min_reviews is None and wants_popular(text):
        parsed.min_reviews = settings.popular_min_reviews
        logger.info("query asks for popular, min_reviews=%d", parsed.min_reviews)

    # Fills, never overrides - the same shape as the rule above. The model was
    # only ever observed returning NO value here, not a wrong one, and an
    # override would break a mixed ask like "single player or co-op" that the
    # model reads correctly.
    if parsed.multiplayer is None and wants_singleplayer(text):
        parsed.multiplayer = False
        logger.info("query asks to play alone, multiplayer=False")

    # Last, and after the rules above on purpose: it reads parsed.multiplayer
    # and parsed.excluded_tags to decide which of the referenced game's tags it
    # is allowed to borrow.
    return apply_reference(parsed, text)
