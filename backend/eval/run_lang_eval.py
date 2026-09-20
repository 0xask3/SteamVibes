"""How much does asking in German cost? A matched-pairs measurement.

    uv run python -m eval.run_lang_eval
    uv run python -m eval.run_lang_eval --parse     # through the query parser

run_eval's EN and DE rows are not a language measurement: German is 37% `core`
against English's 22%, so part of that gap is tier mix, and the per-tier matrix
still leaves target choice. queries_de.yaml holds the SAME 118 targets and
tiers asked in German, so here language is the only variable and the comparison
is paired query by query.

THE 27 ROWS THAT WERE ALREADY GERMAN ARE EXCLUDED: identical in both files,
they would be free ties that drag the average toward zero. That leaves 91
genuine pairs, and the output says so rather than reporting n=118.
"""

import argparse
import logging
import statistics
from pathlib import Path

from app.config import settings
from app.embedding import verify_corpus_complete, verify_corpus_model
from app.query_parser import parse_query
from app.rerank import verify_rerank_model
from app.schemas import ParsedQuery
from app.search import search
from eval.paired import report
from eval.run_eval import Case, load_cases

EN_PATH = Path(__file__).parent / "queries.yaml"
DE_PATH = Path(__file__).parent / "queries_de.yaml"


def recall_of(case: Case, limit: int, parse: bool) -> float:
    parsed = (
        parse_query(case.query) if parse else ParsedQuery(semantic_query=case.query)
    )
    # relax_filters=False for run_eval's reason: recall is measured against a
    # FIXED filter set, and widening whenever a query returns little would
    # report the relaxation as retrieval quality.
    response = search(parsed, limit=limit, relax_filters=False)
    if response.rerank_ms is not None and not response.reranked:
        raise SystemExit(
            f"REFUSING TO REPORT: the cross-encoder fell back on {case.query!r}, "
            "so this arm would be part baseline. Check the WARNING lines above."
        )
    score, _ = case.recall([r.app_id for r in response.results], limit)
    return score


def main() -> None:
    argp = argparse.ArgumentParser(description="EN vs DE on matched targets.")
    argp.add_argument("--limit", type=int, default=10, help="The k in recall@k.")
    argp.add_argument(
        "--parse",
        action="store_true",
        help="Run both arms through the chat model first.",
    )
    args = argp.parse_args()

    logging.basicConfig(level=logging.ERROR)

    verify_corpus_model()
    verify_corpus_complete()
    if settings.rank_method == "rerank":
        verify_rerank_model()

    english = load_cases(EN_PATH)
    german = load_cases(DE_PATH)
    assert len(english) == len(german), "the two sets are not aligned"

    # Only rows that genuinely differ in language. Alignment is asserted rather
    # than assumed: if expect or tier ever drifted between the files, every
    # number below would be a comparison of two different questions.
    pairs: list[tuple[Case, Case]] = []
    for en, de in zip(english, german):
        assert en.expect == de.expect, f"targets differ: {en.query!r} vs {de.query!r}"
        assert en.tier == de.tier, f"tiers differ: {en.query!r} vs {de.query!r}"
        if en.query != de.query:
            pairs.append((en, de))

    mode = "parsed" if args.parse else "semantic only"
    print(f"\n{len(pairs)} matched pairs | recall@{args.limit} | {mode}")
    print("set:   queries.yaml (EN) against queries_de.yaml (DE)")
    print(f"model: {settings.embed_model}  |  rank: {settings.rank_method}")
    print(f"excluded: {len(english) - len(pairs)} rows already German in both files\n")

    en_scores: list[float] = []
    de_scores: list[float] = []
    by_tier: dict[str, tuple[list[float], list[float]]] = {}

    for en, de in pairs:
        en_score = recall_of(en, args.limit, args.parse)
        de_score = recall_of(de, args.limit, args.parse)
        en_scores.append(en_score)
        de_scores.append(de_score)
        tier_en, tier_de = by_tier.setdefault(en.tier, ([], []))
        tier_en.append(en_score)
        tier_de.append(de_score)

        mark = (
            "  " if en_score == de_score else ("DE+" if de_score > en_score else "EN+")
        )
        print(f"{mark} {en.query[:56]:<58}{en_score:>5.0%}{de_score:>7.0%}")

    print("\n" + "=" * 70)
    print(f"OVERALL, n={len(pairs)}")
    report("English", "German", en_scores, de_scores)

    # Per tier, because the tiers measure different things and are not
    # comparable to each other. Cells are small - read these as hints, and the
    # overall row as the result.
    for tier in ("core", "specific", "tail"):
        if tier not in by_tier:
            continue
        tier_en, tier_de = by_tier[tier]
        print(f"\n{tier.upper()}, n={len(tier_en)}")
        report("English", "German", tier_en, tier_de)

    print(
        f"\nper-query mean |EN - DE|: {statistics.mean(abs(a - b) for a, b in zip(en_scores, de_scores)):.1%}"
    )


if __name__ == "__main__":
    main()
