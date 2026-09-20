"""One-line "why this matches" per result, then checked against the database.

The sentence is not the deliverable, the DISCARD RATE is: a plausible sentence
attached to a real game is the hardest kind of wrong to notice. Three
properties make the check mean something, and none is optional:

  The grounding data comes from the DATABASE, never the caller - a verifier fed
  client-supplied tags is checking the model against the client.
  A discard is FINAL. Retrying until it passes converts a measured failure rate
  into a hidden latency cost.
  The fallback is DETERMINISTIC and marked `grounded=False` all the way to the
  UI, so a canned line is never presented as an explanation.

Scope, stated in the README rather than implied away: this catches invented
TAGS. A model that invents a plot detail passes every check below.
"""

import logging
import re
import time
from typing import Any, NamedTuple

from sqlalchemy import select

from app.db import session_scope
from app.llm import chat_json
from app.models import Game
from app.query_parser import get_tag_vocabulary
from app.schemas import Explanation, VerifiedExplanation

logger = logging.getLogger(__name__)

# The full list runs to 20+, and it is votes-ordered by the ingest.
TAGS_PER_GAME = 8

# Enough to write one sentence from. Ten full blurbs would crowd the context.
DESCRIPTION_CHARS = 240

SYSTEM_PROMPT = """You explain why a video game matches a player's search.

For each game you are given, write ONE short sentence - at most 20 words - \
saying why it fits the search. Address the player's words directly.

RULES, and the first two are absolute:
1. You may ONLY mention tags from that game's own "Tags:" list. Never a tag \
from a different game, and never a tag you believe applies but was not listed.
2. Put every tag you mention in `cited_tags`, spelled EXACTLY as it appears in \
that game's list.
3. If a game does not really fit the search, say so plainly. A hedge is better \
than an invention.
4. No marketing language. No "immerse yourself", no exclamation marks.
5. Return one entry per game, using the app_id you were given.

Example for the search "cozy farming game with fishing":
  app_id 413150, Tags: Farming Sim, Relaxing, Pixel Graphics, Fishing
  -> cited_tags: ["Farming Sim", "Fishing", "Relaxing"]
  -> why: "Farming Sim with Fishing, and its Relaxing pace matches 'cozy'."
"""


def _schema() -> dict[str, Any]:
    """JSON schema for a list of Explanation, for Ollama's `format`.

    Wrapped in an object with a required `items` array: a bare top-level array
    gives the model no field name to anchor on, and `required` is explicit for
    the same reason `_REQUIRED_FIELDS` exists in the parser (failures.md #33).
    """
    return {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": Explanation.model_json_schema()}
        },
        "required": ["items"],
    }


class GameRecord(NamedTuple):
    """The verified facts about one game, read straight from `games`."""

    app_id: int
    name: str
    description: str | None
    tags: list[str]


def _load_games(app_ids: list[int]) -> dict[int, GameRecord]:
    """(name, description, tags) per id - the ground truth the model is graded on.

    Plain tuples rather than ORM objects, so nothing outlives the session or
    lazy-loads later.
    """
    with session_scope() as session:
        rows = session.execute(
            select(Game.app_id, Game.name, Game.short_description, Game.tags).where(
                Game.app_id.in_(app_ids)
            )
        ).all()
        return {
            r.app_id: GameRecord(
                r.app_id, r.name, r.short_description, list(r.tags or [])
            )
            for r in rows
        }


def _prompt(query: str, games: list[GameRecord]) -> str:
    lines = [f'The player searched for: "{query}"', "", "Games:"]
    for game in games:
        tags = ", ".join(game.tags[:TAGS_PER_GAME]) or "(none)"
        blurb = (game.description or "").strip()[:DESCRIPTION_CHARS]
        lines.append(f"- app_id {game.app_id}: {game.name}")
        lines.append(f"  Tags: {tags}")
        if blurb:
            lines.append(f"  About: {blurb}")
    return "\n".join(lines)


# A tag inside a NEGATED clause is a denial, not a claim: the prompt asks the
# model to hedge rather than invent, so punishing it for that inflated the rate.
# Any regex over prose needs this guard.
_NEGATION = re.compile(
    r"\b(?:not|no|without|omits?|lacks?|lacking|missing|absent|excludes?|"
    r"isn't|aren't|doesn't|don't|never|nor)\b",
    re.IGNORECASE,
)
# Clause boundaries. The negation has to be in the SAME clause as the tag, or
# "not a puzzle game but it is Souls-like" would suppress a real invention.
_CLAUSE = re.compile(r"[,;.:!?]|\bbut\b|\band\b|\bwhile\b|\bthough\b", re.IGNORECASE)


def _clause_around(text: str, start: int, end: int) -> str:
    """The clause containing [start, end), for the negation test."""
    left = 0
    right = len(text)
    for match in _CLAUSE.finditer(text):
        if match.end() <= start:
            left = match.end()
        elif match.start() >= end:
            right = match.start()
            break
    return text[left:right]


def _prose_tags(why: str, vocabulary: tuple[str, ...]) -> set[str]:
    """Tags CLAIMED in the prose, whether or not they were declared.

    Case-SENSITIVE whole-word matching, so "plenty of action" does not trip
    `Action` while "it is Souls-like" does. A floor on prose hallucination, not
    a complete check.

    Three guards, each found by auditing real discards, and each of which was
    inflating the rate in the direction nobody investigates: overlaps resolve
    LONGEST-FIRST (`Farming` inside `Farming Sim`); a tag in a negated clause is
    a denial; and a SENTENCE-INITIAL single-word tag is grammar, not a citation
    (`Experience` is also an ordinary verb). Multi-word tags still count there.
    """
    spans: list[tuple[int, int, str]] = []
    for tag in vocabulary:
        for match in re.finditer(rf"(?<!\w){re.escape(tag)}(?!\w)", why):
            spans.append((match.start(), match.end(), tag))

    spans.sort(key=lambda s: s[0] - s[1])  # longest first
    kept: list[tuple[int, int, str]] = []
    for start, end, tag in spans:
        if any(k_start <= start and end <= k_end for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, tag))

    claimed = set()
    for start, end, tag in kept:
        if " " not in tag and "-" not in tag:
            before = why[:start].rstrip()
            if not before or before.endswith((".", "!", "?")):
                continue  # sentence-initial single word: capitalisation is grammar
        if _NEGATION.search(_clause_around(why, start, end)):
            continue
        claimed.add(tag)
    return claimed


def _fallback(game: GameRecord, wanted_tags: list[str]) -> str:
    """A deterministic line for when the model's answer was discarded.

    Built from the game's own tags, so it cannot itself be wrong, preferring
    the overlap with what the query asked for.
    """
    tags = game.tags
    overlap = [t for t in tags if t in set(wanted_tags)]
    shown = overlap[:3] or tags[:3]
    if not shown:
        return "Matched on description similarity."
    return "Tagged " + ", ".join(shown) + "."


def explain(
    query: str, app_ids: list[int], wanted_tags: list[str] | None = None
) -> tuple[list[VerifiedExplanation], float]:
    """Explain each game, discarding anything that cites what it should not.

    Never raises: a dead model, a malformed response and a hallucinated tag all
    end at a deterministic line marked `grounded=False`.
    """
    started = time.perf_counter()
    games = _load_games(app_ids)
    wanted = wanted_tags or []
    vocabulary = get_tag_vocabulary()

    ordered = [games[i] for i in app_ids if i in games]
    if not ordered:
        return [], (time.perf_counter() - started) * 1000

    by_id: dict[int, Explanation] = {}
    try:
        raw = chat_json(SYSTEM_PROMPT, _prompt(query, ordered), _schema())
        for item in raw.get("items", []):
            try:
                parsed = Explanation.model_validate(item)
            except Exception:
                # One bad entry must not lose the other nine, and the raw item
                # is what makes a schema drift debuggable.
                logger.warning(
                    "explanation entry failed validation: %r", item, exc_info=True
                )
                continue
            by_id[parsed.app_id] = parsed
    except Exception:
        # Same contract as the parser: log it, degrade, never propagate.
        logger.warning("explanation model call failed; falling back", exc_info=True)

    out: list[VerifiedExplanation] = []
    for game in ordered:
        candidate = by_id.get(game.app_id)
        real = set(game.tags)

        reason: str | None = None
        if candidate is None:
            # A dead model, or one that answered about a game nobody asked
            # about - which is why app_id is checked at all.
            reason = "missing"
        elif not set(candidate.cited_tags) <= real:
            reason = "unlisted_tag"
        elif not _prose_tags(candidate.why, vocabulary) <= real:
            reason = "prose_tag"

        if reason is None and candidate is not None:
            out.append(
                VerifiedExplanation(
                    app_id=game.app_id, why=candidate.why.strip(), grounded=True
                )
            )
        else:
            if reason != "missing":
                logger.warning(
                    "discarded explanation for %s (%s): %r",
                    game.name,
                    reason,
                    candidate.why if candidate else None,
                )
            out.append(
                VerifiedExplanation(
                    app_id=game.app_id,
                    why=_fallback(game, wanted),
                    grounded=False,
                    discard_reason=reason,
                )
            )

    # Ids nobody asked about. Nothing to attach them to, but worth the line:
    # inventing an app_id is why the parser never sees one.
    invented = set(by_id) - {g.app_id for g in ordered}
    if invented:
        logger.warning("model returned unknown app_ids: %s", sorted(invented))

    return out, (time.perf_counter() - started) * 1000

