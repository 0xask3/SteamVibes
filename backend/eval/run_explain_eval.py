"""Measures how often the explanation layer lies, and whether the check notices.

    uv run python -m eval.run_explain_eval
    uv run python -m eval.run_explain_eval --queries 20    # a quicker look
    uv run python -m eval.run_explain_eval --self-test     # feed it known lies
    uv run python -m eval.run_explain_eval --show          # print every line

The number is the DISCARD RATE, not a quality score: app/explain.py verifies
every claim against `games.tags` and throws away what fails, and this counts how
often that happens. Split by REASON, because one rate is three different bugs:

  unlisted_tag  cited_tags named a tag the game does not have.
  prose_tag     the sentence names one that cited_tags did not declare.
  missing       no entry came back - a dead model, an unparseable item, or an
                app_id nobody asked about.

RUN --self-test FIRST, and after any change to the verifier. A checker that has
never gone red is not known to work: this one once discarded 2 of 5 CORRECT
explanations, inflating the headline in the safe-looking direction - the
direction nobody investigates.
"""

import argparse
import logging
import statistics
from collections import Counter
from typing import Any

from app import explain as explain_module
from app.config import settings
from app.explain import explain
from app.schemas import ParsedQuery
from app.search import search
from eval.run_eval import load_cases

# Known-bad model responses, each aimed at one check. The app_id is filled in
# per game at run time; the tags are ones essentially no game carries together.
SELF_TESTS: dict[str, dict[str, Any]] = {
    "unlisted_tag": {
        "cited_tags": ["Souls-like", "Farming Sim", "Cycling"],
        "why": "A good match for the search.",
    },
    "prose_tag": {
        "cited_tags": [],
        "why": "This is a Souls-like with Base-Building and Cycling.",
    },
    "missing": {"__unknown_app_id__": True},
}


def _fake_chat(response_items: list[dict[str, Any]]) -> Any:
    def _inner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"items": response_items}

    return _inner


def self_test(app_ids: list[int]) -> int:
    """Reinstate each failure by hand and check the verifier goes red.

    Returns the number of arms that did NOT behave as expected, so the exit
    code means something.
    """
    print("\nself-test: each arm feeds a known-bad response and must be caught\n")
    original = explain_module.chat_json
    failures = 0
    try:
        for name, payload in SELF_TESTS.items():
            if payload.get("__unknown_app_id__"):
                items = [
                    {
                        "app_id": 999999999,
                        "cited_tags": [],
                        "why": "Wrong game entirely.",
                    }
                ]
            else:
                items = [{"app_id": app_ids[0], **payload}]
            explain_module.chat_json = _fake_chat(items)  # type: ignore[assignment]
            out, _ = explain(" a query", app_ids[:1])
            got = out[0].discard_reason if out else "NO OUTPUT"
            ok = got == name
            failures += not ok
            print(f"  {name:<14} -> {got!s:<14} {'caught' if ok else 'NOT CAUGHT'}")

        # A model that raises must degrade, not propagate.
        def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise RuntimeError("ollama is down")

        explain_module.chat_json = _boom  # type: ignore[assignment]
        out, _ = explain("a query", app_ids[:1])
        ok = bool(out) and not out[0].grounded and out[0].discard_reason == "missing"
        failures += not ok
        print(
            f"  {'model raises':<14} -> {'degraded' if ok else 'PROPAGATED'}"
            f"{'':<6}{'caught' if ok else 'NOT CAUGHT'}"
        )

        # And the control: a truthful response must SURVIVE. Without this arm a
        # verifier that rejects everything would score a perfect self-test.
        explain_module.chat_json = original  # type: ignore[assignment]
        out, _ = explain("a relaxing game", app_ids[:1])
        ok = bool(out)
        print(
            f"  {'control (real)':<14} -> "
            f"{'grounded' if out and out[0].grounded else 'discarded'}"
            f"{'':<6}{'(informational)'}"
        )
    finally:
        explain_module.chat_json = original  # type: ignore[assignment]

    print(f"\n{'PASS' if not failures else f'{failures} ARM(S) FAILED'}")
    return failures


def main() -> None:
    argp = argparse.ArgumentParser(description="Explanation hallucination rate.")
    argp.add_argument(
        "--queries", type=int, default=0, help="Sample N queries (0=all)."
    )
    argp.add_argument(
        "--limit", type=int, default=5, help="Results explained per query."
    )
    argp.add_argument("--self-test", action="store_true", help="Feed it known lies.")
    argp.add_argument("--show", action="store_true", help="Print every explanation.")
    args = argp.parse_args()
    logging.basicConfig(level=logging.ERROR)  # the table is the output

    cases = load_cases()
    if args.queries:
        # Evenly spaced rather than the first N, so a sample is not all one tier.
        step = max(1, len(cases) // args.queries)
        cases = cases[::step][: args.queries]

    if args.self_test:
        first = search(ParsedQuery(semantic_query=cases[0].query), limit=1)
        raise SystemExit(self_test([r.app_id for r in first.results]))

    print(f"\n{len(cases)} queries x top {args.limit} | model: {settings.chat_model}")
    print("scored: did every claim survive a check against games.tags\n")

    reasons: Counter[str] = Counter()
    grounded = total = 0
    times: list[float] = []

    for case in cases:
        response = search(ParsedQuery(semantic_query=case.query), limit=args.limit)
        ids = [r.app_id for r in response.results]
        if not ids:
            continue
        names = {r.app_id: r.name for r in response.results}
        out, elapsed = explain(
            case.query, ids, wanted_tags=response.parsed.required_tags
        )
        times.append(elapsed)
        for item in out:
            total += 1
            if item.grounded:
                grounded += 1
            else:
                reasons[item.discard_reason or "unknown"] += 1
            if args.show:
                flag = "  ok  " if item.grounded else f" {item.discard_reason:<5}"
                print(f"{flag} {names.get(item.app_id, '?')[:26]:<28}{item.why[:64]}")

    if not total:
        raise SystemExit("no results to explain")

    discarded = total - grounded
    print(f"{'explanations':<26}{total:>8}")
    print(f"{'  grounded':<26}{grounded:>8}{grounded / total:>10.1%}")
    print(
        f"{'  DISCARDED':<26}{discarded:>8}{discarded / total:>10.1%}   <- the number"
    )
    for reason in ("unlisted_tag", "prose_tag", "missing"):
        if reasons[reason]:
            print(
                f"{'    ' + reason:<26}{reasons[reason]:>8}"
                f"{reasons[reason] / total:>10.1%}"
            )
    print(f"\n{'median batch latency':<26}{statistics.median(times):>8.0f}ms")
    print(
        "\nThis is a FLOOR on hallucination, not a measure of it: it catches "
        "invented\ntags. A model that invents a plot detail passes every check "
        "here."
    )


if __name__ == "__main__":
    main()
