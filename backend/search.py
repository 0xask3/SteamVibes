"""Search the games database from a terminal.

    uv run python search.py "cozy farming game with fishing"
    uv run python search.py "co-op base builder" --limit 20 --threshold 50
    uv run python search.py "relaxing puzzle" --json

Presentation only. The ranking lives in app/search.py, so the API in Weekend 2
serves the identical logic rather than a second copy of it.
"""

import argparse
import textwrap

from app.schemas import SearchResponse, SearchResult
from app.search import search

DESCRIPTION_WIDTH = 76


def format_price(result: SearchResult) -> str:
    if result.is_free:
        return "Free"
    if result.list_price_usd is None:
        return "-"
    return f"${result.list_price_usd}"


def print_result(rank: int, result: SearchResult) -> None:
    print(f"\n{rank:>2}. {result.name}   [{result.score:.3f}]")

    facts = [format_price(result), f"{result.total_reviews:,} reviews"]
    if result.positive_ratio is not None:
        facts.append(f"{result.positive_ratio * 100:.0f}% positive")
    if result.platforms:
        facts.append("/".join(result.platforms))
    print(f"    {'  |  '.join(facts)}")

    if result.tags:
        print(f"    {', '.join(result.tags)}")

    if result.short_description:
        wrapped = textwrap.shorten(
            result.short_description, width=DESCRIPTION_WIDTH, placeholder=" ..."
        )
        print(f"    {wrapped}")


def print_response(response: SearchResponse) -> None:
    print(f'\nquery: "{response.query}"   threshold: >{response.threshold} reviews')

    if not response.results:
        print("\nno results. Try lowering --threshold.")
        return

    for rank, result in enumerate(response.results, start=1):
        print_result(rank, result)

    print(
        f"\n{response.returned} results  |  "
        f"embed {response.embed_ms:.0f}ms  |  query {response.query_ms:.0f}ms"
    )

    # Never let this pass silently: it means the filter starved the vector
    # index of candidates, not that only this many games matched.
    if response.under_delivered:
        print(
            f"\nWARNING: asked for {response.requested}, got {response.returned}. "
            "The filter is selective enough to starve the index - widen it or "
            "raise hnsw.iterative_scan limits."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic search over Steam games.")
    parser.add_argument("query", help="What you feel like playing.")
    parser.add_argument("--limit", type=int, default=10, help="Results to return.")
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Minimum total reviews. Defaults to REVIEW_THRESHOLD from .env.",
    )
    parser.add_argument("--json", action="store_true", help="Emit raw JSON.")
    args = parser.parse_args()

    response = search(args.query, limit=args.limit, threshold=args.threshold)

    if args.json:
        print(response.model_dump_json(indent=2))
    else:
        print_response(response)


if __name__ == "__main__":
    main()
