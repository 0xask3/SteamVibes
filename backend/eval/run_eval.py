"""recall@10 over eval/queries.yaml, split English vs German.

    uv run python -m eval.run_eval
    uv run python -m eval.run_eval --parse          # with the query parser
    uv run python -m eval.run_eval --limit 20       # recall@20
    uv run python -m eval.run_eval --misses         # show what was not found

recall@10 = (expected app_ids found in top 10) / (expected app_ids), averaged
PER QUERY rather than pooled. Ground truth is deliberately incomplete - it
holds answers confident enough that missing one is a real failure - so the
absolute number is a baseline to move, not a percentage of correctness.
"""

import argparse
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.embedding import verify_corpus_complete, verify_corpus_model
from app.query_parser import parse_query
from app.rerank import verify_rerank_model
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
        # `core` (short genre labels), `specific` (a detailed description with
        # one right answer) and `tail` (the same, but the answer has 30-300
        # reviews). Only `tail` can see what a ranking change deletes.
        self.tier: str = raw.get("tier", "core")

    def recall(self, returned: list[int], limit: int) -> tuple[float, set[int]]:
        """Fraction of expected ids in the top `limit`, plus the ones missed."""
        found = self.expect & set(returned[:limit])
        return len(found) / len(self.expect), self.expect - found


def load_cases(path: Path = QUERIES_PATH) -> list[Case]:
    with path.open(encoding="utf-8") as handle:
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
    # A DIFFERENT FILE IS A DIFFERENT MEASUREMENT, never a variant of this one:
    # recall compares configs on a FIXED set, and never across sets.
    argp.add_argument(
        "--queries",
        type=Path,
        default=QUERIES_PATH,
        help="Query set to run. Defaults to eval/queries.yaml, which is the "
        "fixed set every published number comes from. eval/queries_de.yaml is "
        "the same targets asked in German.",
    )
    # Per-query scores, so two runs can be compared PAIRED rather than by their
    # averages. eval/compare_runs.py consumes these.
    argp.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="Write per-query scores here as JSON, for eval/compare_runs.py.",
    )
    args = argp.parse_args()

    logging.basicConfig(level=logging.ERROR)  # the table is the output

    # A table produced against a corpus embedded by a different model, or half
    # embedded, is worse than no table: it looks exactly like a config result.
    verify_corpus_model()
    verify_corpus_complete()
    # Same invisible mismatch one layer out - a model that loads but cannot
    # score reproduces the baseline under its own label.
    if settings.rank_method == "rerank":
        verify_rerank_model()

    cases = load_cases(args.queries)
    names: dict[int, str] = {}
    scores: dict[str, list[float]] = {"en": [], "de": []}
    elapsed: list[float] = []
    rerank_times: list[float] = []
    fell_back = 0
    misses: list[tuple[Case, set[int]]] = []
    tiers: dict[str, list[float]] = {"core": [], "specific": [], "tail": []}
    cells: dict[tuple[str, str], list[float]] = {}
    returned_reviews: list[int] = []
    per_query: list[dict[str, object]] = []

    mode_label = "parsed" if args.parse else "semantic only"
    threshold = settings.review_threshold if args.threshold is None else args.threshold
    print(f"\n{len(cases)} queries | recall@{args.limit} | {mode_label}")
    # The SET belongs here as much as the model does: queries_de.yaml has the
    # same size, tiers and targets, so nothing else on screen tells them apart.
    print(f"set:   {args.queries.name}")
    # Self-labelling, because a table pasted into NOTES.md without these is not
    # reproducible - "18.3%" means nothing without the ranking line.
    rank: str = settings.rank_method
    if settings.rank_method == "log":
        rank = f"log (w={settings.popularity_weight})"
    elif settings.rank_method == "rrf":
        rank = f"rrf (w={settings.popularity_weight}, k={settings.rrf_k})"
    elif settings.rank_method == "rerank":
        rank = (
            f"rerank {settings.rerank_model} "
            f"(w={settings.popularity_weight}, k={settings.rrf_k})"
        )
    print(f"model: {settings.embed_model}  |  review threshold: {threshold:,}")
    print("relax: OFF (recall needs a fixed filter set)")
    print(
        f"rank:  {rank}  |  ef_search: {settings.hnsw_ef_search}  |  "
        f"pool: {settings.rerank_candidates}\n"
    )
    print(f"{'':4}{'query':<52}{'lang':<6}{'recall':>7}")
    print("-" * 70)

    for case in cases:
        parsed = (
            parse_query(case.query)
            if args.parse
            else ParsedQuery(semantic_query=case.query)
        )
        start = time.perf_counter()
        # relax_filters=False is not a detail: a harness that widened filters
        # when a query returned little would report relaxation as retrieval
        # quality. --parse would trigger it.
        response = search(
            parsed,
            limit=args.limit,
            threshold=args.threshold,
            relax_filters=False,
        )
        elapsed.append(time.perf_counter() - start)
        if response.rerank_ms is not None:
            rerank_times.append(response.rerank_ms)
            if not response.reranked:
                fell_back += 1

        recall, missed = case.recall([r.app_id for r in response.results], args.limit)
        per_query.append({"query": case.query, "tier": case.tier, "recall": recall})
        scores[case.lang].append(recall)
        tiers[case.tier].append(recall)
        cells.setdefault((case.tier, case.lang), []).append(recall)
        returned_reviews.extend(r.total_reviews for r in response.results)
        if missed:
            misses.append((case, missed))

        flag = "  " if recall == 1.0 else ("~ " if recall > 0 else "! ")
        mark = {"specific": "*", "tail": "+"}.get(case.tier, " ")
        print(f"{flag}{mark} {case.query[:50]:<52}{case.lang:<6}{recall:>6.0%}")

    print("-" * 70)

    # A run where ANY query fell back is not a weaker measurement of this model,
    # it is the baseline wearing this model's label. Three fallbacks out of 118
    # would move a number by more than the reproducibility floor.
    if fell_back:
        raise SystemExit(
            f"\nREFUSING TO REPORT: the cross-encoder failed on {fell_back} of "
            f"{len(cases)} queries and those fell back to the SQL ordering, so "
            "this table would be part baseline. Check the WARNING lines above."
        )

    def row(tag: str, values: list[float]) -> None:
        """One summary line, padded so every percentage lands in a column."""
        head = f"  recall@{args.limit} ({tag}):"
        print(f"{head:<32}{statistics.mean(values):>6.1%}")

    for lang in ("en", "de"):
        if scores[lang]:
            row(f"{lang.upper()}, n={len(scores[lang])}", scores[lang])

    # The tiers are NOT comparable to each other - core understates quality,
    # because its ground truth names 2-3 famous games out of hundreds that would
    # do. Compare a tier against itself across configs, and judge a RANKING
    # change on `tail`: core and specific targets sit above the 93rd percentile
    # by reviews, so recall on them rises with a popularity weight whatever it
    # costs. See failures.md #26-28.
    for tier in ("core", "specific", "tail"):
        if tiers[tier]:
            row(f"{tier}, n={len(tiers[tier])}", tiers[tier])

    row("overall", scores["en"] + scores["de"])

    # The EN/DE rows above are NOT a language measurement: German is 37% `core`
    # against English's 22%, so part of any gap is tier mix. Read this matrix,
    # and treat a single row as a hint - one query is 10-12 points in these
    # cells. eval/run_lang_eval.py is the unconfounded comparison.
    print()
    print(f"{'  by tier and language':<24}{'EN':>14}{'DE':>14}{'gap':>9}")
    for tier in ("core", "specific", "tail"):
        en, de = cells.get((tier, "en"), []), cells.get((tier, "de"), [])
        if not en or not de:
            continue
        e, d = statistics.mean(en), statistics.mean(de)
        label = f"    {tier}"
        print(
            f"{label:<24}{e:>8.1%} (n={len(en):>2}){d:>8.1%} (n={len(de):>2}){e - d:>8.1%}"
        )
    print()

    # The counter-metric, and it needs no labels at all - which is the point,
    # since recall@k cannot see what a popularity weight DELETES: ground truth
    # is a list of games somebody thought of, and people think of famous games.
    # This just asks how obscure the returned results are. See failures.md #26.
    if returned_reviews:
        under_1k = sum(n < 1000 for n in returned_reviews) / len(returned_reviews)
        median_revs = int(statistics.median(returned_reviews))
        print(f"  median reviews returned:{median_revs:>9,}")
        print(f"  results under 1k reviews:{under_1k:>8.0%}")
    print(f"  median search latency:{statistics.median(elapsed) * 1000:>10.0f}ms")
    # Split out because the reranker's whole trade is recall against latency.
    # p95 too: usually-fast-sometimes-slow is a different product from evenly
    # slow.
    if rerank_times:
        ordered = sorted(rerank_times)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(
            f"    of which reranking:{statistics.median(rerank_times):>12.0f}ms"
            f"   (p95 {p95:.0f}ms)"
        )

    if args.dump is not None:
        # The CONFIG travels with the scores, so a file found a week later still
        # says what produced it - and compare_runs.py can refuse two sets.
        args.dump.write_text(
            json.dumps(
                {
                    "set": args.queries.name,
                    "embed_model": settings.embed_model,
                    "rank_method": settings.rank_method,
                    "rerank_model": (
                        settings.rerank_model
                        if settings.rank_method == "rerank"
                        else None
                    ),
                    "popularity_weight": settings.popularity_weight,
                    "parsed": args.parse,
                    "limit": args.limit,
                    "queries": per_query,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"\n  wrote {len(per_query)} per-query scores to {args.dump}")

    if args.misses and misses:
        print("\nmissed:")
        for case, missed in misses:
            listed = ", ".join(f"{name_of(i, names)} ({i})" for i in sorted(missed))
            print(f"  {case.query}\n      {listed}")
            if case.note:
                print(f"      known: {case.note}")


if __name__ == "__main__":
    main()
