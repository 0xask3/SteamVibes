"""Search the games database from a terminal.

    uv run python search.py "cozy farming game with fishing"
    uv run python search.py "co-op base builder under 20 dollars on linux" --parse
    uv run python search.py "something for a kid" --max-age 7 --exclude-tag Violent

Presentation only. The ranking lives in app/search.py, so the API in the next
step serves identical logic rather than a second copy.

--parse asks the chat model to fill the ParsedQuery; the flags fill the same
object by hand and override anything it decided. Keeping the unparsed path is
what makes the parser's contribution measurable rather than assumed.
"""

import argparse
import logging
import textwrap

from app.embedding import verify_corpus_model
from app.query_parser import parse_query
from app.schemas import ParsedQuery, SearchResponse, SearchResult
from app.search import search

DESCRIPTION_WIDTH = 76


def format_price(result: SearchResult) -> str:
    if result.is_free:
        return "Free"
    if result.list_price_usd is None:
        return "-"
    return f"${result.list_price_usd}"


def print_result(rank: int, result: SearchResult) -> None:
    # Show the ranking key alongside the similarity whenever they differ.
    # Similarity alone reads as a bug once a popularity term is ordering the
    # list: the printed numbers are not monotonic and nothing on screen says
    # why. Equal values mean rank_method is "none".
    marks = f"{result.score:.3f}"
    if result.rank_score != result.score:
        marks = f"sim {result.score:.3f}  rank {result.rank_score:.4f}"
    print(f"\n{rank:>2}. {result.name}   [{marks}]")

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
    header = f'\nquery: "{response.parsed.semantic_query}"'
    header += f"   threshold: >{response.threshold} reviews"
    print(header)

    # Always show what was actually applied - a filter that silently did not
    # apply looks exactly like one that found nothing.
    filters = response.parsed.describe()
    if filters:
        print(f"filters: {'  '.join(filters)}")

    if response.unknown_tags:
        print(
            f"\nWARNING: unknown tag(s): {', '.join(response.unknown_tags)}. "
            "These match nothing, so results will be empty. Tag names are "
            "exact - 'Base-Building', not 'Base Building'."
        )

    if not response.results:
        print("\nno results. Loosen a filter or lower --threshold.")
        return

    for rank, result in enumerate(response.results, start=1):
        print_result(rank, result)

    print(
        f"\n{response.returned} results  |  "
        f"embed {response.embed_ms:.0f}ms  |  query {response.query_ms:.0f}ms"
    )

    # Never silent: this means the filter starved the vector index of
    # candidates, not that only this many games matched.
    if response.under_delivered:
        print(
            f"\nWARNING: asked for {response.requested}, got {response.returned}. "
            "The filters are selective enough to starve the index - widen them "
            "or raise hnsw.iterative_scan limits."
        )


def build_parsed_query(args: argparse.Namespace) -> ParsedQuery:
    multiplayer: bool | None = None
    if args.multiplayer:
        multiplayer = True
    elif args.singleplayer:
        multiplayer = False

    # --parse asks the model; without it the query is treated as pure vibe and
    # only explicit flags filter. Keeping the unparsed path is what makes the
    # parser's contribution measurable rather than assumed.
    if args.parse:
        base = parse_query(args.query, model=args.model)
    else:
        base = ParsedQuery(semantic_query=args.query)

    # Explicit flags win over anything the model decided. This is BUILD_PLAN's
    # editable filter chips in CLI form: the parser's choices are visible and
    # correctable rather than silently applied.
    if args.max_price is not None:
        base.max_price_usd = args.max_price
    if args.min_price is not None:
        base.min_price_usd = args.min_price
    if args.tag:
        base.required_tags = args.tag
    if args.exclude_tag:
        base.excluded_tags = args.exclude_tag
    if args.platform:
        base.platforms = args.platform
    if args.after is not None:
        base.released_after = args.after
    if multiplayer is not None:
        base.multiplayer = multiplayer
    if args.max_age is not None:
        base.max_required_age = args.max_age

    return base


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

    parser.add_argument("--max-price", type=float, help="Max list price in USD.")
    parser.add_argument("--min-price", type=float, help="Min list price in USD.")
    parser.add_argument(
        "--tag",
        action="append",
        help="Require this tag. Repeatable. Exact match, e.g. 'Base-Building'.",
    )
    parser.add_argument(
        "--exclude-tag", action="append", help="Exclude this tag. Repeatable."
    )
    parser.add_argument(
        "--platform",
        action="append",
        choices=["windows", "mac", "linux"],
        help="Must run on this. Repeatable, and ANDed.",
    )
    parser.add_argument("--after", type=int, help="Released in or after this year.")
    parser.add_argument("--max-age", type=int, help="Max required_age.")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--multiplayer", action="store_true", help="Multiplayer only.")
    group.add_argument("--singleplayer", action="store_true", help="No multiplayer.")

    parser.add_argument(
        "--parse",
        action="store_true",
        help="Use the chat model to extract filters from the query.",
    )
    parser.add_argument(
        "--model", help="Override CHAT_MODEL for --parse, e.g. qwen3.5:4b."
    )
    parser.add_argument("--json", action="store_true", help="Emit raw JSON.")
    args = parser.parse_args()

    # WARNING and above to stderr, so the parser fallback is visible rather
    # than silent. CLAUDE.md: the fallback AND the log line.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Before the first embed call, not after: a model mismatch is silent at
    # every other layer, so it has to be checked rather than noticed.
    verify_corpus_model()

    response = search(
        build_parsed_query(args), limit=args.limit, threshold=args.threshold
    )

    if args.json:
        print(response.model_dump_json(indent=2))
    else:
        print_response(response)


if __name__ == "__main__":
    main()
