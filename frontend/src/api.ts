/**
 * The three calls this app makes. Plain fetch, no client library: one less
 * layer between a request and something readable in a network tab.
 */

import type {
  ExplainRequest,
  ExplainResponse,
  SearchRequest,
  SearchResponse,
  StatsResponse,
} from "./types";

// 127.0.0.1, not localhost: on Windows localhost resolves to IPv6 ::1 first
// and stalls ~2s per connection. Same reason the backend pins it. See NOTES.md.
const API_URL = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

export async function search(request: SearchRequest): Promise<SearchResponse> {
  const response = await fetch(`${API_URL}/api/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    // 503 carries a readable detail from the API; anything else gets the code.
    const detail = await response
      .json()
      .then((body) => body.detail as string | undefined)
      .catch(() => undefined);
    throw new Error(detail ?? `Search failed (HTTP ${response.status})`);
  }

  return (await response.json()) as SearchResponse;
}

/**
 * Explanations for results already on screen.
 *
 * A SECOND request on purpose: an LLM call inside search would hold the whole
 * list for a sentence nobody has scrolled to. Sends app_ids rather than games,
 * because a check against browser-supplied tags checks the model against the
 * browser. Never throws - a failure must leave the page exactly as it was.
 */
export async function explain(request: ExplainRequest): Promise<ExplainResponse | null> {
  try {
    const response = await fetch(`${API_URL}/api/explain`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    if (!response.ok) return null;
    return (await response.json()) as ExplainResponse;
  } catch {
    return null;
  }
}

/**
 * Server-side latency percentiles, for the diagnostics panel. Never throws,
 * for explain()'s reason: a stats call that took the page down would be worse
 * than no stats at all. An in-memory read, so calling it per search is free.
 */
export async function stats(): Promise<StatsResponse | null> {
  try {
    const response = await fetch(`${API_URL}/api/stats`);
    if (!response.ok) return null;
    return (await response.json()) as StatsResponse;
  } catch {
    return null;
  }
}
