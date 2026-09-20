/**
 * Mirrors backend/app/schemas.py, hand-written because codegen would be more
 * machinery than four shapes save. Field order matches the Python model, where
 * it IS load-bearing, so the two files stay diffable by eye.
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
   * Set by the backend's referenced-game lookup, never by the model:
   * "like elden ring" appends its tags to semantic_query and lands its name
   * here. excluded_app_ids fills only when the query asked to leave it out.
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
   * What the results were ordered by, once popularity is folded in. Equals
   * `score` under RANK_METHOD "none", and NOT comparable across methods.
   */
  rank_score: number;

  /**
   * A STRING, not a number: Pydantic serialises Decimal as a string to avoid
   * float rounding, so .toFixed() on it would throw.
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
   * failed and fell back, so `reranked` is what says whether it worked.
   */
  rerank_ms: number | null;
  reranked: boolean;

  /** null when no chat model ran - i.e. the caller supplied `parsed`. */
  parse_ms: number | null;

  /** Filters starved the vector index; fewer results than asked for. */
  under_delivered: boolean;

  /**
   * Filters widened to fill this page; empty is the normal case. `parsed`
   * above is the RELAXED set, so the chips show the query that ran and this is
   * the diff explaining it. The UI must render it: silently widening a stated
   * constraint is worse than a short page.
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
 * database. `grounded: false` means the model's line was DISCARDED for citing
 * a tag the game lacks, and `why` is built from the game's own tags instead.
 * The flag has to reach the UI - a canned sentence presented as an explanation
 * is the failure the verification exists to prevent.
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
 * One stage's latency over the server's window. `p95` is null below
 * `min_p95_samples`, where it would literally be the maximum - a wrong label,
 * not an imprecise number. Render the reason, never a blank.
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
 * GET /api/stats. Read the scope before quoting anything: `window` counts
 * REQUESTS, not time; the figures are per-process; and they see API traffic
 * only, so they are not the measurement `run_eval` prints.
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
