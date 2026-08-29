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
from functools import lru_cache

from sqlalchemy import select

from app.db import session_scope
from app.llm import chat_json
from app.models import GameTag
from app.schemas import ParsedQuery

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
            schema=ParsedQuery.model_json_schema(),
            model=model,
        )
    except Exception:
        # Transport failure, missing model, non-JSON content. Log it with the
        # query so it can be reproduced, then fall back.
        logger.warning("parser call failed for %r, falling back", text, exc_info=True)
        return ParsedQuery(semantic_query=text)

    try:
        parsed = ParsedQuery.model_validate(raw)
    except Exception:
        # Schema-valid JSON can still be semantically wrong. Log the raw output
        # rather than just the exception - that is what makes it debuggable.
        logger.warning(
            "parser returned unusable output for %r: %r", text, raw, exc_info=True
        )
        return ParsedQuery(semantic_query=text)

    # The model was told to copy tags exactly. It will not always.
    parsed.required_tags = _resolve_tags(parsed.required_tags, vocabulary)
    parsed.excluded_tags = _resolve_tags(parsed.excluded_tags, vocabulary)

    # An empty semantic_query would embed nothing useful. Prefer the raw text.
    if not parsed.semantic_query.strip():
        logger.info("parser emptied semantic_query for %r, using raw text", text)
        parsed.semantic_query = text

    return parsed
