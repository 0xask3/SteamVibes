/**
 * The two calls this app makes.
 *
 * Plain fetch, no client library. The backend talks to Ollama over direct HTTP
 * for the same reason - one less layer between a request and something you can
 * read in a network tab.
 */

import type {
  ExplainRequest,
  ExplainResponse,
  SearchRequest,
  SearchResponse,
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
 * A SECOND request on purpose. Search already costs ~1.1s of reranking, and
 * folding an LLM call into it would delay the whole result list for a sentence
 * nobody has scrolled to yet. Results render, then these arrive.
 *
 * Sends app_ids rather than the games: the backend re-reads name and tags from
 * its own database, because a check run against numbers the browser supplied
 * would be checking the model against the browser.
 *
 * Never throws. Explanations decorate a list that is already correct and
 * useful, so a failure here must leave the page exactly as it was.
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
