"""Compare two run_eval dumps, PAIRED, with a sign test and a bootstrap.

    uv run python -m eval.run_eval --dump a.json          # arm A
    RANK_METHOD=rrf uv run python -m eval.run_eval --dump b.json
    uv run python -m eval.compare_runs a.json b.json

TWO REFUSALS, both of them the point of the file:

  Dumps from DIFFERENT QUERY SETS. Recall is comparable across configs on a
  fixed set and never across sets, so this is not a weaker comparison - it is
  not a comparison.

  Dumps whose queries do not line up row for row. The value of a paired test is
  that ~100 of 118 queries are discarded as ties; pairing the wrong rows
  silently converts that strength into noise.
"""

import argparse
import json
from pathlib import Path

from eval.paired import report


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def label(run: dict) -> str:
    """What actually distinguishes this arm, in one line."""
    parts = [run["rank_method"]]
    if run.get("rerank_model"):
        parts.append(Path(run["rerank_model"]).name)
    parts.append(f"w={run['popularity_weight']}")
    if run.get("parsed"):
        parts.append("parsed")
    return " ".join(parts)


def main() -> None:
    argp = argparse.ArgumentParser(description="Paired comparison of two dumps.")
    argp.add_argument("a", type=Path, help="Baseline dump.")
    argp.add_argument("b", type=Path, help="Dump to test against the baseline.")
    args = argp.parse_args()

    run_a, run_b = load(args.a), load(args.b)

    if run_a["set"] != run_b["set"]:
        raise SystemExit(
            f"REFUSING TO COMPARE: {args.a.name} ran {run_a['set']} and "
            f"{args.b.name} ran {run_b['set']}. Recall is comparable across "
            "CONFIGS on a fixed set, never across sets - two sets side by side "
            "is not a weaker comparison, it is not a comparison."
        )
    if run_a["embed_model"] != run_b["embed_model"]:
        print(
            f"  NOTE: different embedding models "
            f"({run_a['embed_model']} vs {run_b['embed_model']}). That is a "
            "corpus change as well as a config change - read accordingly.\n"
        )

    queries_a = run_a["queries"]
    queries_b = run_b["queries"]
    if len(queries_a) != len(queries_b):
        raise SystemExit(
            f"REFUSING TO COMPARE: {len(queries_a)} queries against "
            f"{len(queries_b)}. These are not the same measurement."
        )
    for left, right in zip(queries_a, queries_b):
        if left["query"] != right["query"]:
            raise SystemExit(
                "REFUSING TO COMPARE: the dumps are not aligned row for row - "
                f"{left['query']!r} against {right['query']!r}. A paired test on "
                "misaligned rows discards the ties that give it its power."
            )

    a_label, b_label = label(run_a), label(run_b)
    print(f"\n{len(queries_a)} queries | set: {run_a['set']}")
    print(f"  A = {a_label}")
    print(f"  B = {b_label}\n")

    a_scores = [float(q["recall"]) for q in queries_a]
    b_scores = [float(q["recall"]) for q in queries_b]

    print("=" * 70)
    print(f"OVERALL, n={len(a_scores)}")
    report("A", "B", a_scores, b_scores)

    # Per tier. These are hints, not results - the tiers are not comparable to
    # each other, and `core` labels 2-3 famous games out of hundreds that would
    # satisfy the query, so it cannot price a ranking change at all.
    tiers = sorted({str(q["tier"]) for q in queries_a})
    for tier in tiers:
        picked = [i for i, q in enumerate(queries_a) if q["tier"] == tier]
        if not picked:
            continue
        print(f"\n{tier.upper()}, n={len(picked)}")
        report("A", "B", [a_scores[i] for i in picked], [b_scores[i] for i in picked])

    moved = [
        (queries_a[i]["query"], a_scores[i], b_scores[i])
        for i in range(len(a_scores))
        if a_scores[i] != b_scores[i]
    ]
    # The queries that moved ARE the result. Everything else is a tie, and a
    # difference resting on a handful of queries should be read as such.
    print(f"\n{len(moved)} of {len(a_scores)} queries moved:")
    for query, before, after in moved:
        arrow = "B+" if after > before else "A+"
        print(f"  {arrow} {query[:56]:<58}{before:>5.0%}{after:>7.0%}")


if __name__ == "__main__":
    main()
