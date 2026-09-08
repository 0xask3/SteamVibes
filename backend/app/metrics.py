"""Per-request timings and fallback counts, kept in memory for /api/stats.

BUILD_PLAN's Weekend 4 item 6. The pipeline now prices five stages separately -
parse, relax, embed, query, rerank - plus a second request for explanations, and
until now they existed only per-response. Nobody could ask what the
cross-encoder costs at p95, which is the question its entire trade turns on.

A deque with a maxlen behind a lock. No table, no migration, no dependency, and
the memory cost is fixed at `stats_window` small records however long the
process runs.

FOUR THINGS THIS DELIBERATELY DOES NOT PRETEND TO BE, all of them stated in the
response body as well as here, because a stats endpoint that misrepresents its
own scope is precisely the "wrong result that looks right" this project exists
to avoid:

  IT IS API TRAFFIC ONLY. Recording happens in app/main.py at the endpoint, not
  inside search(), so the CLI and run_eval accumulate nothing. That keeps the
  ranking path undiluted - the same reason app/games.py is not in app/search.py -
  but it also means this p95 and run_eval's median are not the same measurement
  and must not be quoted as though they were.

  THE WINDOW IS THE LAST N REQUESTS, NOT A TIME WINDOW. "p95" therefore drifts
  with traffic and says nothing about any particular hour.

  IT IS PER-PROCESS AND RESETS ON RESTART. One uvicorn worker today; under
  several, each would hold its own slice and a caller would see whichever
  answered.

  NOTHING IS EXCLUDED. A cold search that spent ~22s paging 6.6GB of chat model
  into VRAM stays in the window and will dominate p95 at small n. Dropping slow
  requests to make a latency number look better is the dishonesty this project
  exists to avoid; `n` is the mitigation, not a filter.
"""

import threading
import time
from collections import Counter, deque
from collections.abc import Iterable

from app.config import settings
from app.schemas import EndpointStats, StageStats, StatsResponse

# Below this many samples the "95th percentile" IS the maximum, so reporting one
# is a wrong label rather than an imprecise number - p95 comes back null instead.
#
# 21 IS EXACT, NOT A ROUND NUMBER. With nearest-rank the index is
# min(n-1, int(n*0.95)), and that only stops being the last element at n=21:
# at n=20 it is int(19.0)=19=n-1, i.e. the maximum. A first pass used 20 and the
# guard silently did nothing at exactly the boundary it existed to police - the
# check script prints p95 beside max at several n so the two cannot drift apart
# again unnoticed.
#
# A module constant, not a setting. CLAUDE.md's "a setting somebody has to type
# is a decision somebody made" is about RERANK_TRUST_REMOTE_CODE, which is a
# genuine choice; this is a rule about what a word means, and making it tunable
# would be offering to turn it off.
MIN_P95_SAMPLES = 21

# The stages, in pipeline order, which is also the order they are rendered in.
# `total` last because it is not a stage - it is the wall clock the user waits.
_SEARCH_STAGES = (
    "parse_ms",
    "relax_ms",
    "embed_ms",
    "query_ms",
    "rerank_ms",
    "total_ms",
)
_EXPLAIN_STAGES = ("total_ms",)

_STARTED_AT = time.time()

_lock = threading.Lock()
_records: dict[str, deque[dict[str, float]]] = {
    "search": deque(maxlen=settings.stats_window),
    "explain": deque(maxlen=settings.stats_window),
}
_counters: Counter[str] = Counter()


def record(endpoint: str, timings: dict[str, float]) -> None:
    """Append one request's stage timings.

    A stage that did not run is ABSENT from `timings`, never zero. rerank_ms is
    None whenever RANK_METHOD is not `rerank` - which is every containerised
    deployment - and parse_ms is None on the chip path, where the caller
    supplied the filters. Recording either as 0.0 would drag that stage's p50
    toward nothing and read as "free".
    """
    with _lock:
        _records[endpoint].append(timings)


def note(event: str, count: int = 1) -> None:
    """Count a fallback or a degraded path.

    Monotonic since process start, deliberately: a fallback is rare and its
    total is more useful than its rate over a sliding window. Called from
    app/query_parser.py as well as the endpoints, because a parse failure is
    invisible downstream - it returns a bare ParsedQuery, which is exactly what
    a query with no constraints also produces.

    THESE COUNT EVENTS, NOT REQUESTS, so do not divide one by `search.n`.
    Measured: with a bad CHAT_MODEL, one search produced `parse_call_failed: 2`
    - because _warm_models() at startup parses too, and that call really did
    fail. Counting it is the right answer (a parser broken at boot should be
    visible before anyone searches) but it makes the number a count of parser
    CALLS that fell back, which is not the same denominator as searches.
    """
    with _lock:
        _counters[event] += count


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank, matching eval/run_eval.py exactly.

    Same expression on purpose. A p95 computed here with an interpolating method
    would differ from the harness's by a few ms for no reason anybody could
    later reconstruct.
    """
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def _stage(rows: Iterable[dict[str, float]], key: str) -> StageStats | None:
    values = sorted(row[key] for row in rows if key in row)
    if not values:
        return None
    return StageStats(
        n=len(values),
        p50=round(_percentile(values, 0.50), 1),
        # None, not the maximum wearing a p95 label. See MIN_P95_SAMPLES.
        p95=(
            round(_percentile(values, 0.95), 1)
            if len(values) >= MIN_P95_SAMPLES
            else None
        ),
        max=round(values[-1], 1),
    )


def _endpoint(rows: list[dict[str, float]], stages: tuple[str, ...]) -> EndpointStats:
    return EndpointStats(
        n=len(rows),
        # Absent rather than zeroed: a stage that never ran has no p50, and a
        # 0.0 there would read as a stage that is free.
        stages={key: stat for key in stages if (stat := _stage(rows, key)) is not None},
    )


def snapshot() -> StatsResponse:
    """Everything /api/stats returns.

    The lock covers the copy, not the arithmetic - sorting a few hundred floats
    while holding it would serialise requests behind a stats call.
    """
    with _lock:
        rows = {name: list(buffer) for name, buffer in _records.items()}
        counters = dict(_counters)

    return StatsResponse(
        window=settings.stats_window,
        uptime_s=round(time.time() - _STARTED_AT, 1),
        min_p95_samples=MIN_P95_SAMPLES,
        search=_endpoint(rows["search"], _SEARCH_STAGES),
        explain=_endpoint(rows["explain"], _EXPLAIN_STAGES),
        fallbacks=counters,
    )
