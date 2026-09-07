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
        # Three tiers, measuring three different things. "core" is short genre
        # labels answered by famous games; "specific" is a detailed description
        # with one right answer; "tail" is the same but the answer has 36-293
        # reviews. Only "tail" can see what a ranking change deletes - the other
        # two are entirely above the 93rd percentile by review count, so recall
        # on them rises with a popularity weight regardless of its cost. See the
        # queries.yaml headers.
        self.tier: str = raw.get("tier", "core")

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

    # A results table produced against a corpus embedded by a different model is
    # worse than no table: it looks exactly like a model comparison. Checked
    # here because this file's whole job is deciding whether a change helped.
    verify_corpus_model()
    verify_corpus_complete()
    # Same class of invisible mismatch, one layer out: TEI serves whatever model
    # its container started with, the backend cannot tell, and a container left
    # running from the previous bake-off arm answers every request happily. That
    # difference would be written into a table as a model result.
    if settings.rank_method == "rerank":
        verify_rerank_model()

    cases = load_cases()
    names: dict[int, str] = {}
    scores: dict[str, list[float]] = {"en": [], "de": []}
    elapsed: list[float] = []
    rerank_times: list[float] = []
    fell_back = 0
    misses: list[tuple[Case, set[int]]] = []
    tiers: dict[str, list[float]] = {"core": [], "specific": [], "tail": []}
    cells: dict[tuple[str, str], list[float]] = {}
    returned_reviews: list[int] = []

    mode_label = "parsed" if args.parse else "semantic only"
    threshold = settings.review_threshold if args.threshold is None else args.threshold
    print(f"\n{len(cases)} queries | recall@{args.limit} | {mode_label}")
    # Self-labelling: these decide the numbers below, and a results table
    # pasted into NOTES.md without them is not reproducible. The ranking line
    # matters as much as the model: "18.3%" means nothing without knowing
    # whether a popularity term produced it.
    rank: str = settings.rank_method
    if settings.rank_method == "log":
        rank = f"log (w={settings.popularity_weight})"
    elif settings.rank_method == "rrf":
        rank = f"rrf (w={settings.popularity_weight}, k={settings.rrf_k})"
    elif settings.rank_method == "rerank":
        # The reranker model belongs on this line for the same reason the embed
        # model does: a table pasted into NOTES.md without it is not reproducible.
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
        # relax_filters=False is NOT a detail. Recall is measured against a
        # FIXED filter set, and a harness that quietly widened filters whenever
        # a query returned little would report the relaxation as retrieval
        # quality. The no-parse path would never trigger it - no filters means
        # 55,120 rows pass - but --parse would, and a number that only
        # sometimes includes a second mechanism is the worst kind.
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
    # it is a measurement of the baseline wearing this model's label. Refuse it
    # rather than print a table somebody will paste into NOTES.md. Partial
    # counts matter too: 3 fallbacks out of 118 would move a number by more than
    # the reproducibility floor and look like a result.
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

    # NOT comparable to each other. Core queries are short genre labels whose
    # ground truth names 2-3 famous games out of hundreds that would satisfy the
    # query, so core recall understates quality; "specific" and "tail" have
    # exactly one right answer by construction. Compare a tier against itself
    # across configs, never one tier against another.
    #
    # Read "tail" when judging a ranking change. Core and specific targets are
    # all above the 93rd percentile by review count, so recall on them rises
    # with a popularity weight whatever it costs - that is not evidence, it is
    # the ground truth's bias paid back to itself. "tail" targets sit at the
    # 37th-75th percentile of the searchable corpus, so a weight that buries the
    # long tail shows up here as a fall. Its absolute value is not a quality
    # number; only its slope is. See failures.md #26-28.
    for tier in ("core", "specific", "tail"):
        if tiers[tier]:
            row(f"{tier}, n={len(tiers[tier])}", tiers[tier])

    row("overall", scores["en"] + scores["de"])

    # The EN/DE rows above are NOT a language measurement on their own: the two
    # sets do not have the same tier mix. German is 37% core queries against
    # English's 22%, and core scores about a quarter of what the other tiers do,
    # so a chunk of any aggregate gap is composition rather than language. Read
    # this matrix instead. Measured on arctic at w=0.20 the aggregate gap was
    # 24.3 points while the per-tier gaps were 4.2 / 30.2 / 12.5 - same data,
    # three different stories. Cells are small (8-10 German queries per tier),
    # so one query is 10-12 points here; treat a single row as a hint.
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

    # Counter-metric, and the honest one. recall@k cannot see what a popularity
    # weight DELETES, because ground truth is a list of games somebody thought
    # of - and people think of famous games. Worse, a labelled long-tail tier
    # does not fix that on its own: if the query paraphrases the game's own
    # description the target sits at cosine rank ~1, where a popularity term is
    # too small to dislodge it, so the tier reports "no harm" by construction.
    #
    # This needs no labels and no query authorship, so neither bias reaches it.
    # It just asks how obscure the returned results actually are. Context: the
    # review threshold already removes 58% of the corpus, and of the 55,120
    # games that remain 69% have under 200 reviews. If those never come back,
    # the long tail is not being searched - which is the product. See
    # failures.md #26.
    if returned_reviews:
        under_1k = sum(n < 1000 for n in returned_reviews) / len(returned_reviews)
        median_revs = int(statistics.median(returned_reviews))
        print(f"  median reviews returned:{median_revs:>9,}")
        print(f"  results under 1k reviews:{under_1k:>8.0%}")
    print(f"  median search latency:{statistics.median(elapsed) * 1000:>10.0f}ms")
    # Split out because the reranker's entire trade is recall against latency,
    # and the line above hides it inside a total that also carries the embed
    # call. p95 as well as median: a reranker that is usually fast and
    # occasionally slow is a different product from one that is evenly slow.
    if rerank_times:
        ordered = sorted(rerank_times)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        print(
            f"    of which reranking:{statistics.median(rerank_times):>12.0f}ms"
            f"   (p95 {p95:.0f}ms)"
        )

    if args.misses and misses:
        print("\nmissed:")
        for case, missed in misses:
            listed = ", ".join(f"{name_of(i, names)} ({i})" for i in sorted(missed))
            print(f"  {case.query}\n      {listed}")
            if case.note:
                print(f"      known: {case.note}")


if __name__ == "__main__":
    main()
