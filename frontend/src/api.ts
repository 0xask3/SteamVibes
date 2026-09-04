/**
 * The one call this app makes.
 *
 * Plain fetch, no client library. The backend talks to Ollama over direct HTTP
 * for the same reason - one less layer between a request and something you can
 * read in a network tab.
 */

import type { SearchRequest, SearchResponse } from "./types";

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
