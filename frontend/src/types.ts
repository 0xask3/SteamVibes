/**
 * Mirrors backend/app/schemas.py. Kept hand-written rather than generated:
 * there are four shapes here and a codegen step would be more machinery than
 * it saves.
 *
 * Field order in ParsedQuery matches the Python model, where the order is
 * load-bearing for the LLM's constrained generation. It does not matter here,
 * but keeping them aligned makes the two files diffable by eye.
 */

export type Platform = "windows" | "mac" | "linux";

export interface ParsedQuery {
  max_price_usd: number | null;
  min_price_usd: number | null;
  required_tags: string[];
  excluded_tags: string[];
  platforms: Platform[];
  released_after: number | null;
  multiplayer: boolean | null;
  max_required_age: number | null;

  /** Minimum user reviews. Set from "popular" and friends, detected in code. */
  min_reviews: number | null;

  /**
   * Set by the backend's referenced-game lookup, never by the model. When the
   * query names a well-known game ("like elden ring"), its tags are appended
   * to semantic_query and its name lands here. excluded_app_ids is populated
   * only when the query also asked to leave that game out.
   */
  reference_game: string | null;
  excluded_app_ids: number[];

  semantic_query: string;
}

export interface SearchResult {
  app_id: number;
  name: string;
  short_description: string | null;
  score: number;

  /**
   * The key the results were actually ordered by, once the popularity term is
   * folded in. Equals `score` when RANK_METHOD is "none". Not comparable
   * across rank methods - "rrf" values sit near 1/k, not on the 0-1
   * similarity scale.
   */
  rank_score: number;

  /**
   * A STRING, not a number. Pydantic serialises Decimal as a string to avoid
   * float rounding, so "9.99" arrives rather than 9.99. Calling .toFixed() on
   * it would throw.
   */
  list_price_usd: string | null;
  is_free: boolean;

  total_reviews: number;
  positive_ratio: number | null;
  tags: string[];
  platforms: string[];
}

export interface SearchResponse {
  parsed: ParsedQuery;
  results: SearchResult[];

  /** Tags matching nothing real. Shown, never swallowed. */
  unknown_tags: string[];

  requested: number;
  returned: number;
  threshold: number;

  embed_ms: number;
  query_ms: number;

  /** null when the relaxation ladder did not run (RELAX_FILTERS off). */
  relax_ms: number | null;

  /**
   * null when no cross-encoder ran. A number means it did, INCLUDING when it
   * failed and search fell back to the SQL ordering - time spent on a dead
   * model is still time spent, so `reranked` is what says whether it worked.
   */
  rerank_ms: number | null;
  reranked: boolean;

  /** null when no chat model ran - i.e. the caller supplied `parsed`. */
  parse_ms: number | null;

  /** Filters starved the vector index; fewer results than asked for. */
  under_delivered: boolean;

  /**
   * Filters widened to fill this page, in the order they were given up. Empty
   * is the normal case.
   *
   * NOTE that `parsed` above is the RELAXED set - what actually ran - so the
   * chips reflect the real query. This list is the diff that explains why they
   * differ from what was typed, and the UI must show it: silently widening a
   * constraint the user stated is worse than returning few results.
   */
  relaxed: RelaxationStep[];
}

export interface RelaxationStep {
  field: string;
  was: string;
  now: string;
  /** Pre-rendered by the backend; phrasing differs per field. */
  note: string;
}

export interface SearchRequest {
  query: string;
  /** Present = use these filters verbatim, skip the LLM. The chip path. */
  parsed?: ParsedQuery;
  limit?: number;
  threshold?: number;
}

/**
 * One "why this matches" line, after the backend checked it against the
 * database.
 *
 * `grounded: false` means a model DID write something and it was thrown away
 * for citing a tag the game does not have - `why` is then a deterministic line
 * built from the game's own tags. The flag has to reach the UI: a canned
 * sentence presented as an explanation is precisely the failure the
 * verification exists to prevent.
 */
export interface VerifiedExplanation {
  app_id: number;
  why: string;
  grounded: boolean;
  discard_reason: string | null;
}

export interface ExplainRequest {
  query: string;
  app_ids: number[];
  /** Only picks which of the game's own tags the fallback line shows. */
  wanted_tags?: string[];
}

export interface ExplainResponse {
  explanations: VerifiedExplanation[];
  elapsed_ms: number;
}

/**
 * One stage's latency over the server's window. See GET /api/stats.
 *
 * `p95` is null until the server has `min_p95_samples` requests, because below
 * that the 95th percentile is literally the maximum and labelling the maximum
 * "p95" is wrong rather than merely imprecise. Render the gap, never a blank.
 */
export interface StageStats {
  n: number;
  p50: number;
  p95: number | null;
  max: number;
}

export interface EndpointStats {
  n: number;
  /** Keyed by SearchResponse field name. A stage that never ran is ABSENT. */
  stages: Record<string, StageStats>;
}

/**
 * GET /api/stats.
 *
 * Read the scope before quoting anything: `window` counts REQUESTS, not time;
 * the figures cover one server process and reset when it restarts; and they
 * see API traffic only, so they are not the same measurement as the median
 * `run_eval` prints.
 */
export interface StatsResponse {
  window: number;
  uptime_s: number;
  min_p95_samples: number;
  search: EndpointStats;
  explain: EndpointStats;
  /** Monotonic since server start, not windowed. */
  fallbacks: Record<string, number>;
}
