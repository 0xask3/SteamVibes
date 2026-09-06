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

  /** null when no chat model ran - i.e. the caller supplied `parsed`. */
  parse_ms: number | null;

  /** Filters starved the vector index; fewer results than asked for. */
  under_delivered: boolean;
}

export interface SearchRequest {
  query: string;
  /** Present = use these filters verbatim, skip the LLM. The chip path. */
  parsed?: ParsedQuery;
  limit?: number;
  threshold?: number;
}
