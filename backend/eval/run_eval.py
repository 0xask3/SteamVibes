"""recall@10 over eval/queries.yaml, split English vs German.

    uv run python -m eval.run_eval
    uv run python -m eval.run_eval --parse          # with the query parser
    uv run python -m eval.run_eval --limit 20       # recall@20
    uv run python -m eval.run_eval --misses         # show what was not found

This is the number that decides whether a change helped. Two hypotheses were
killed by measurement this project (failures.md #19, #20) and one prompt edit
was reverted after it silently cost a filter (#22) - but all of that used
compare_parsers.py, which only inspects the ParsedQuery and never looks at
which games come back. This closes that gap.

recall@10 = (expected app_ids found in top 10) / (expected app_ids)

Averaged per query, not pooled, so a query expecting one game counts the same
as one expecting three. Ground truth is deliberately incomplete: `expect` holds
results confident enough that missing one is a real failure, not every game
that would be reasonable. So treat the absolute number as a baseline to move,
not as a percentage of correctness.

Throwaway/eval category per CLAUDE.md: if it runs, it's fine.
"""

import argparse
import logging
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.query_parser import parse_query
from app.schemas import ParsedQuery
from app.search import search

QUERIES_PATH = Path(__file__).parent / "queries.yaml"


class Case:
    """One labelled query."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.query: str = raw["query"]
        self.lang: str = raw.get("lang", "en")
        self.expect: set[int] = set(raw["expect"])
        self.note: str | None = raw.get("from")

    def recall(self, returned: list[int], limit: int) -> tuple[float, set[int]]:
        """Fraction of expected ids in the top `limit`, plus the ones missed."""
        found = self.expect & set(returned[:limit])
        return len(found) / len(self.expect), self.expect - found


def load_cases() -> list[Case]:
    with QUERIES_PATH.open(encoding="utf-8") as handle:
        return [Case(raw) for raw in yaml.safe_load(handle)]


def name_of(app_id: int, cache: dict[int, str]) -> str:
    """Names for the misses report, looked up once each."""
    if app_id not in cache:
        from sqlalchemy import select

        from app.db import session_scope
        from app.models import Game

        with session_scope() as session:
            found = session.scalar(select(Game.name).where(Game.app_id == app_id))
        cache[app_id] = found or f"<unknown {app_id}>"
    return cache[app_id]


def main() -> None:
    argp = argparse.ArgumentParser(description="recall@k over the labelled set.")
    argp.add_argument(
        "--parse",
        action="store_true",
        help="Run each query through the chat model first. Slower; measures the "
        "parser's contribution rather than raw semantic search.",
    )
    argp.add_argument("--limit", type=int, default=10, help="The k in recall@k.")
    argp.add_argument("--threshold", type=int, default=None, help="Min reviews.")
    argp.add_argument("--misses", action="store_true", help="List what was missed.")
    args = argp.parse_args()

    logging.basicConfig(level=logging.ERROR)  # the table is the output

    cases = load_cases()
    names: dict[int, str] = {}
    scores: dict[str, list[float]] = {"en": [], "de": []}
    elapsed: list[float] = []
    misses: list[tuple[Case, set[int]]] = []

    label = "parsed" if args.parse else "semantic only"
    threshold = settings.review_threshold if args.threshold is None else args.threshold
    print(f"\n{len(cases)} queries | recall@{args.limit} | {label}")
    # Self-labelling: these two decide the numbers below, and a results table
    # pasted into NOTES.md without them is not reproducible.
    print(f"model: {settings.embed_model}  |  review threshold: {threshold:,}\n")
    print(f"{'':4}{'query':<52}{'lang':<6}{'recall':>7}")
    print("-" * 70)

    for case in cases:
        parsed = (
            parse_query(case.query)
            if args.parse
            else ParsedQuery(semantic_query=case.query)
        )
        start = time.perf_counter()
        response = search(parsed, limit=args.limit, threshold=args.threshold)
        elapsed.append(time.perf_counter() - start)

        recall, missed = case.recall([r.app_id for r in response.results], args.limit)
        scores[case.lang].append(recall)
        if missed:
            misses.append((case, missed))

        flag = "  " if recall == 1.0 else ("~ " if recall > 0 else "! ")
        print(f"{flag}  {case.query[:50]:<52}{case.lang:<6}{recall:>6.0%}")

    print("-" * 70)
    for lang in ("en", "de"):
        if scores[lang]:
            mean = statistics.mean(scores[lang])
            print(f"  recall@{args.limit} ({lang.upper()}, n={len(scores[lang])}): {mean:.1%}")

    overall = statistics.mean(scores["en"] + scores["de"])
    print(f"  recall@{args.limit} (overall):      {overall:.1%}")
    print(f"  median search latency:    {statistics.median(elapsed) * 1000:.0f}ms")

    if args.misses and misses:
        print("\nmissed:")
        for case, missed in misses:
            listed = ", ".join(f"{name_of(i, names)} ({i})" for i in sorted(missed))
            print(f"  {case.query}\n      {listed}")
            if case.note:
                print(f"      known: {case.note}")


if __name__ == "__main__":
    main()
