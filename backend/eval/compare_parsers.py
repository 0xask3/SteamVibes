"""Run the same queries through several chat models and compare the filters.

    uv run python -m eval.compare_parsers
    uv run python -m eval.compare_parsers --models qwen2.5:7b qwen2.5:14b

Answers "which model should parse queries" with evidence rather than opinion.
Prints each model's ParsedQuery per query, flags disagreements, and times them,
because the parser sits in the request path - a better parse that costs two
extra seconds may not be worth it.

Throwaway/eval category per CLAUDE.md: if it runs, it's fine.
"""

import argparse
import logging
import time

from app.config import settings
from app.query_parser import parse_query
from app.schemas import ParsedQuery

# Chosen to cover the failure modes in eval/failures.md rather than to flatter
# the models: negation, hard constraints, jargon, age gating, and German.
QUERIES = [
    "co-op base builder under 20 dollars that runs on linux",
    "cheap relaxing puzzle games, nothing scary",
    "game where the map changes every run",
    "fun game for a 7 year old that isn't violent",
    "something like dark souls but not fantasy",
    "free multiplayer shooter released after 2020",
    "cozy farming game with fishing",
    "gemütliches Aufbauspiel für zwei",
    "entspanntes Spiel zum Abschalten",
    "rundenbasierte Strategie mit Koop-Modus",
]


def summarise(parsed: ParsedQuery) -> str:
    filters = parsed.describe()
    shown = "  ".join(filters) if filters else "(none)"
    return f'"{parsed.semantic_query}"\n        filters: {shown}'


def main() -> None:
    argp = argparse.ArgumentParser(description="Compare parser models.")
    argp.add_argument(
        "--models",
        nargs="+",
        default=[settings.chat_model],
        help="Models to compare, e.g. qwen2.5:7b qwen2.5:14b",
    )
    argp.add_argument("--verbose", action="store_true", help="Show parser logs.")
    args = argp.parse_args()

    # Parser logs are noise here unless something is being debugged - the
    # comparison itself is the output.
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)

    # One throwaway parse per model before timing anything. The first call to a
    # model pays its load into VRAM - measured at 4.93s against ~0.77s warm,
    # which landed entirely on whichever model happened to go first and made
    # the averages meaningless.
    print("warming up...", end="", flush=True)
    for model in args.models:
        parse_query("warmup", model=model)
    print(" done")

    totals: dict[str, float] = {model: 0.0 for model in args.models}
    disagreements = 0

    for query in QUERIES:
        print(f"\n{'=' * 78}\n{query}\n{'-' * 78}")
        seen: list[str] = []

        for model in args.models:
            start = time.perf_counter()
            parsed = parse_query(query, model=model)
            elapsed = time.perf_counter() - start
            totals[model] += elapsed

            print(f"  {model:<14} {elapsed:>5.2f}s  {summarise(parsed)}")
            seen.append(parsed.model_dump_json())

        if len(set(seen)) > 1:
            disagreements += 1
            print("  ^ models disagree")

    print(f"\n{'=' * 78}\nqueries: {len(QUERIES)}   disagreements: {disagreements}")
    for model, total in totals.items():
        print(f"  {model:<14} {total / len(QUERIES):.2f}s average")


if __name__ == "__main__":
    main()
