"""Per-request timings and fallback counts, kept in memory for /api/stats.

A deque with a maxlen behind a lock: no table, no migration, and a fixed memory
cost however long the process runs.

FOUR THINGS IT DOES NOT PRETEND TO BE, stated in the response body as well,
because a stats endpoint that misrepresents its own scope is the "wrong result
that looks right" this project exists to avoid:

  API TRAFFIC ONLY - recording happens at the endpoint, so the CLI and run_eval
  accumulate nothing and this p95 is not run_eval's median.
  THE LAST N REQUESTS, NOT A TIME WINDOW, so it drifts with traffic.
  PER-PROCESS, and it resets on restart.
  NOTHING IS EXCLUDED - a cold search that paid 22s of model load stays in the
  window; `n` is the mitigation, not a filter.
"""

import threading
import time
from collections import Counter, deque
from collections.abc import Iterable

from app.config import settings
from app.schemas import EndpointStats, StageStats, StatsResponse

# Below this the "95th percentile" IS the maximum, so p95 comes back null - a
# wrong label, not an imprecise number.
#
# 21 IS EXACT: with nearest-rank the index is min(n-1, int(n*0.95)), which only
# stops being the last element at n=21. A first pass used 20 and the guard
# silently did nothing at the boundary it existed to police. Assert p95 < max,
# do not reason about it.
#
# A module constant, not a setting: making it tunable would offer to turn it off.
MIN_P95_SAMPLES = 21

# Pipeline order, which is also render order. `total` last: it is not a stage
# but the wall clock the user waits.
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

    A stage that did not run is ABSENT from `timings`, never zero: a 0.0 would
    drag that stage's p50 toward nothing and read as "free".
    """
    with _lock:
        _records[endpoint].append(timings)


def note(event: str, count: int = 1) -> None:
    """Count a fallback or a degraded path.

    Monotonic since process start: a fallback is rare, and a rate over a
    sliding window would quietly heal itself.

    THESE COUNT EVENTS, NOT REQUESTS, so never divide one by `search.n` -
    _warm_models() parses at startup, so one search can report two failures.
    """
    with _lock:
        _counters[event] += count


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank, the same expression eval/run_eval.py uses.

    An interpolating method here would differ from the harness's by a few ms
    for no reason anybody could later reconstruct.
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
        # Absent rather than zeroed: a stage that never ran has no p50.
        stages={key: stat for key in stages if (stat := _stage(rows, key)) is not None},
    )


def snapshot() -> StatsResponse:
    """Everything /api/stats returns.

    The lock covers the copy, not the arithmetic: sorting while holding it
    would serialise requests behind a stats call.
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
