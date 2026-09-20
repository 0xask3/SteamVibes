"""Scores the PARSER against labelled expectations, not against recall.

    uv run python -m eval.run_parse_eval
    uv run python -m eval.run_parse_eval --reps 1        # quick look
    uv run python -m eval.run_parse_eval --show-failures # what went wrong

`run_eval` cannot referee a parser change: not one of its 118 queries names a
price, platform, year, age or game, so every filter the parser extracts can only
shrink the candidate set. Recall punishes extraction and can never reward it -
strip the tag arrays out of the schema and `--parse` scores exactly the no-parse
baseline. So this scores what recall cannot see:

  MISSED    a constraint the query stated and the parser did not return.
  INVENTED  a constraint the query never stated - caught without extra labels,
            because any scalar not named in `expect` (or waived in `allow`)
            must come back null.

REPS ARE NOT OPTIONAL: the parser is deterministic within a run and NOT across
runs at temperature=0, so this defaults to 3 and reports a `flaky` column. A
flaky field is not a passing field. Tags are scored leniently and kept out of
the headline - the model picks different but defensible tags run to run.
See failures.md #32-35.
"""

import argparse
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from app.config import settings
from app.query_parser import parse_query, wants_popular
from app.schemas import ParsedQuery

CASES_PATH = Path(__file__).parent / "parse_cases.yaml"

# Scored strictly. Anything here that a case does not name in `expect` must come
# back null or empty - that is the invention check, and it needs no labels.
SCALAR_FIELDS = (
    "max_price_usd",
    "min_price_usd",
    "platforms",
    "released_after",
    "multiplayer",
    "max_required_age",
    "min_reviews",
)
# Derived in code from the title lookup rather than by the model, so they are
# scored but never required-null: a case says `excludes: true` or says nothing.
DERIVED_FIELDS = ("reference_game",)


class Case:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.query: str = raw["query"]
        self.lang: str = raw.get("lang", "en")
        self.expect: dict[str, Any] = raw.get("expect") or {}
        self.allow: set[str] = set(raw.get("allow") or ())
        self.tags: list[str] = raw.get("tags") or []
        self.excludes: bool | None = raw.get("excludes")
        # A documented limitation, kept in the file and out of the score:
        # dropping hard cases is how a harness starts flattering itself. If one
        # starts passing, remove the marker.
        self.known_gap: bool = bool(raw.get("known_gap"))

    def scored_fields(self) -> list[str]:
        """Every field this case has an opinion about."""
        fields = [f for f in SCALAR_FIELDS if f not in self.allow]
        fields += [f for f in DERIVED_FIELDS if f in self.expect]
        return fields

    def wanted(self, field: str) -> Any:
        """The expected value: what `expect` says, otherwise "not set"."""
        return self.expect.get(field)


def load_cases() -> list[Case]:
    with CASES_PATH.open(encoding="utf-8") as handle:
        return [Case(raw) for raw in yaml.safe_load(handle)]


def actual(parsed: ParsedQuery, field: str) -> Any:
    value = getattr(parsed, field)
    # [] and None both mean "not set"; normalise so the comparison is one rule.
    return None if value in ([], "") else value


def matches(got: Any, want: Any, field: str) -> bool:
    if want is None:
        return got is None
    if field == "reference_game":
        # Steam names carry ™/® and edition suffixes; the label is the stem.
        return got is not None and str(want).lower() in str(got).lower()
    if isinstance(want, list):
        return sorted(got or []) == sorted(want)
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return float(got) == float(want)
    return bool(got == want)


def main() -> None:
    argp = argparse.ArgumentParser(description="Score the parser against labels.")
    argp.add_argument(
        "--reps",
        type=int,
        default=3,
        help="Parses per case. The parser varies across runs (failures.md #33), "
        "so 1 is a quick look rather than a result.",
    )
    argp.add_argument("--show-failures", action="store_true", help="List each miss.")
    argp.add_argument("--model", default=None, help="Override CHAT_MODEL.")
    args = argp.parse_args()
    logging.basicConfig(level=logging.ERROR)  # the table is the output

    cases = load_cases()
    model = args.model or settings.chat_model
    print(f"\n{len(cases)} cases x {args.reps} reps | parser: {model}")
    print("scored: did it extract what was stated, and nothing that was not\n")

    correct: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    invented: Counter[str] = Counter()
    flaky: Counter[str] = Counter()
    total: Counter[str] = Counter()
    failures: list[str] = []
    tag_hits = tag_cases = 0
    leaks = leak_total = 0
    gap_total = gap_passing = 0
    gaps_now_passing: list[str] = []
    excl_ok = excl_total = 0

    # One warm-up parse: the first call to a model pays its load into VRAM.
    parse_query("warmup", model=args.model)

    for case in cases:
        runs = [parse_query(case.query, model=args.model) for _ in range(args.reps)]

        if case.known_gap:
            gap_total += 1
            passing = all(
                matches(actual(p, f), case.wanted(f), f)
                for p in runs
                for f in case.scored_fields()
            )
            gap_passing += passing
            if passing:
                gaps_now_passing.append(case.query)
            continue

        for field in case.scored_fields():
            want = case.wanted(field)
            got = [actual(p, field) for p in runs]
            ok = [matches(g, want, field) for g in got]
            total[field] += 1
            if len({str(g) for g in got}) > 1:
                flaky[field] += 1
            if all(ok):
                correct[field] += 1
                continue
            # Which kind of wrong? Missing a stated constraint and inventing an
            # unstated one are different bugs with different fixes.
            if want is None:
                invented[field] += 1
            else:
                missed[field] += 1
            if args.show_failures:
                failures.append(
                    f"  {field:<18} want {want!r:<22} got {got[0]!r:<22} {case.query[:44]}"
                )

        # A constraint that became a filter must stop steering the vector
        # (failures.md #34): the field check above passes happily while the
        # query vector still carries the word.
        if "min_reviews" in case.expect:
            leak_total += 1
            if any(wants_popular(p.semantic_query) for p in runs):
                leaks += 1
                if args.show_failures:
                    failures.append(
                        f"  {'popularity leak':<18} semantic_query still says it: "
                        f"{runs[0].semantic_query[:40]!r}"
                    )

        if case.tags:
            tag_cases += 1
            if any(set(case.tags) & set(p.required_tags) for p in runs):
                tag_hits += 1
        if case.excludes is not None:
            excl_total += 1
            if all(bool(p.excluded_app_ids) == case.excludes for p in runs):
                excl_ok += 1

    print(f"{'field':<20}{'correct':>10}{'missed':>9}{'invented':>10}{'flaky':>8}")
    print("-" * 57)
    for field in SCALAR_FIELDS + DERIVED_FIELDS:
        if not total[field]:
            continue
        print(
            f"{field:<20}{correct[field]:>6}/{total[field]:<3}{missed[field]:>9}"
            f"{invented[field]:>10}{flaky[field]:>8}"
        )
    c, t = sum(correct.values()), sum(total.values())
    print("-" * 57)
    print(f"{'fields correct':<20}{c:>6}/{t:<3}{c / t:>28.1%}")
    print(f"{'  missed':<20}{sum(missed.values()):>10}")
    print(f"{'  invented':<20}{sum(invented.values()):>10}")
    print(f"{'  flaky across reps':<20}{sum(flaky.values()):>10}")
    if excl_total:
        print(f"\n{'exclusion applied':<20}{excl_ok:>6}/{excl_total}")
    if leak_total:
        print(
            f"{'constraint leaked':<20}{leaks:>6}/{leak_total}"
            "   filter set, but the word is still in semantic_query"
        )
    if gap_total:
        print(f"{'known gaps':<20}{gap_total - gap_passing:>6}/{gap_total}"
              "   still failing, as documented; excluded from the score")
        for q in gaps_now_passing:
            print(f"    NOW PASSING - drop the known_gap marker: {q[:52]}")
    if tag_cases:
        print(f"{'tags (lenient)':<20}{tag_hits:>6}/{tag_cases}"
              "   at least one expected tag, reported not scored")
    if failures:
        print("\nfailures:")
        for line in failures:
            print(line)


if __name__ == "__main__":
    main()
