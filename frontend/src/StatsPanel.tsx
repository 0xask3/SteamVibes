/**
 * Server-side latency, from GET /api/stats.
 *
 * BUILD_PLAN item 6 asks for "a small panel". Small is the point: the reranker
 * is bought with latency, and a cost nobody can see is a cost nobody can argue
 * about. Collapsed by default because it is a diagnostic, not a feature.
 *
 * The design constraint that shaped every line below: THESE NUMBERS ARE THE
 * EASIEST IN THE PROJECT TO QUOTE WRONGLY. So `n` sits beside every figure, a
 * withheld p95 renders as a reason rather than a blank, and the scope - last N
 * requests, this process, API traffic only - is on screen rather than in a
 * docstring nobody reading the panel will open.
 */

import type { StatsResponse } from "./types";

/** Display names, in pipeline order. Keys are SearchResponse field names. */
const STAGES: [key: string, label: string][] = [
  ["parse_ms", "parse"],
  ["relax_ms", "relax"],
  ["embed_ms", "embed"],
  ["query_ms", "query"],
  ["rerank_ms", "rerank"],
  ["total_ms", "total"],
];

/** Plain-English names for the counters. Unknown keys fall through as-is. */
const FALLBACKS: Record<string, string> = {
  parse_call_failed: "parser unreachable",
  parse_bad_output: "parser output unusable",
  rerank_fell_back: "reranker failed, SQL order used",
  search_failed_embedding: "search failed: embedding service",
  search_failed_database: "search failed: database",
  relaxed: "filters widened",
  under_delivered: "page not filled",
  explanations_total: "explanations written",
  explanations_ungrounded: "explanations discarded",
};

export function StatsPanel({ stats }: { stats: StatsResponse }) {
  const rows = STAGES.filter(([key]) => key in stats.search.stages);
  const counters = Object.entries(stats.fallbacks).filter(([, n]) => n > 0);

  return (
    <details className="stats">
      <summary>
        Server timings · {stats.search.n}{" "}
        {stats.search.n === 1 ? "search" : "searches"} recorded
      </summary>

      <table>
        <thead>
          <tr>
            <th>stage</th>
            <th>n</th>
            <th>p50</th>
            <th>p95</th>
            <th>max</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([key, label]) => {
            const stage = stats.search.stages[key];
            return (
              <tr key={key}>
                <td>{label}</td>
                <td>{stage.n}</td>
                <td>{stage.p50.toFixed(0)}ms</td>
                {/* Never a blank. A missing p95 has a reason and the reason is
                    the interesting part - below this many samples the 95th
                    percentile IS the maximum, so the label would be a lie. */}
                <td>
                  {stage.p95 === null ? (
                    <span
                      className="stats-gap"
                      title={
                        `Withheld until ${stats.min_p95_samples} requests. ` +
                        "Below that the 95th percentile is just the largest " +
                        "value, so calling it p95 would be wrong rather than " +
                        "merely rough."
                      }
                    >
                      needs {stats.min_p95_samples}
                    </span>
                  ) : (
                    `${stage.p95.toFixed(0)}ms`
                  )}
                </td>
                <td>{stage.max.toFixed(0)}ms</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {stats.explain.stages.total_ms && (
        <p className="stats-line">
          explanations: {stats.explain.stages.total_ms.p50.toFixed(0)}ms p50 over{" "}
          {stats.explain.n} {stats.explain.n === 1 ? "request" : "requests"}{" "}
          (a second request, after results render)
        </p>
      )}

      {counters.length > 0 && (
        <p className="stats-line">
          {/* Events, NOT requests - do not read these as a rate over the n
              above. The server warms the models at startup, and a parser
              broken at boot counts one there before anyone has searched. */}
          <span
            title="Counts of events since the server started, not a rate over the requests above. The startup model warm-up parses too, so a broken parser counts one before any search."
            className="stats-gap"
          >
            since start
          </span>
          :{" "}
          {counters
            .map(([key, n]) => `${FALLBACKS[key] ?? key} ${n}`)
            .join(" · ")}
        </p>
      )}

      <p className="stats-scope">
        Last {stats.window} requests, not a time window. One server process,
        reset {Math.round(stats.uptime_s / 60)} min ago at restart. API traffic
        only — not the same measurement as the eval harness&rsquo;s median, and
        nothing is excluded, so one cold start dominates p95 while n is small.
      </p>
    </details>
  );
}
