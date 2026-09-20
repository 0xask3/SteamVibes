/**
 * Turns a ParsedQuery into removable chips: the TypeScript half of
 * ParsedQuery.describe() in backend/app/schemas.py, worded the same so the
 * browser and the terminal print the same sentence.
 *
 * Each chip's `remove` returns a NEW ParsedQuery rather than mutating - React
 * needs a fresh object, and the result is posted straight back to the API.
 */

import type { ParsedQuery } from "./types";

export interface Chip {
  key: string;
  label: string;
  remove: (parsed: ParsedQuery) => ParsedQuery;
}

/** "$20" and "$19.50", never "$20.00" or "$19.5". */
function money(value: number): string {
  return `$${Number.isInteger(value) ? value : value.toFixed(2)}`;
}

export function chipsFor(parsed: ParsedQuery): Chip[] {
  const chips: Chip[] = [];

  if (parsed.min_price_usd !== null) {
    chips.push({
      key: "min_price",
      label: `≥ ${money(parsed.min_price_usd)}`,
      remove: (p) => ({ ...p, min_price_usd: null }),
    });
  }

  if (parsed.max_price_usd !== null) {
    chips.push({
      key: "max_price",
      // 0 means the user asked for free, which reads better than "≤ $0".
      label: parsed.max_price_usd === 0 ? "Free" : `≤ ${money(parsed.max_price_usd)}`,
      remove: (p) => ({ ...p, max_price_usd: null }),
    });
  }

  for (const platform of parsed.platforms) {
    chips.push({
      key: `platform:${platform}`,
      label: platform === "mac" ? "macOS" : platform[0].toUpperCase() + platform.slice(1),
      remove: (p) => ({ ...p, platforms: p.platforms.filter((x) => x !== platform) }),
    });
  }

  for (const tag of parsed.required_tags) {
    chips.push({
      key: `tag:${tag}`,
      label: tag,
      remove: (p) => ({ ...p, required_tags: p.required_tags.filter((x) => x !== tag) }),
    });
  }

  for (const tag of parsed.excluded_tags) {
    chips.push({
      key: `not:${tag}`,
      label: `not ${tag}`,
      remove: (p) => ({ ...p, excluded_tags: p.excluded_tags.filter((x) => x !== tag) }),
    });
  }

  if (parsed.released_after !== null) {
    chips.push({
      key: "released_after",
      label: `after ${parsed.released_after}`,
      remove: (p) => ({ ...p, released_after: null }),
    });
  }

  if (parsed.multiplayer !== null) {
    chips.push({
      key: "multiplayer",
      label: parsed.multiplayer ? "Multiplayer" : "Singleplayer",
      remove: (p) => ({ ...p, multiplayer: null }),
    });
  }

  if (parsed.max_required_age !== null) {
    chips.push({
      key: "max_age",
      label: `age ≤ ${parsed.max_required_age}`,
      remove: (p) => ({ ...p, max_required_age: null }),
    });
  }

  if (parsed.min_reviews !== null) {
    chips.push({
      key: "min_reviews",
      label: `≥ ${parsed.min_reviews.toLocaleString()} reviews`,
      remove: (p) => ({ ...p, min_reviews: null }),
    });
  }

  if (parsed.excluded_app_ids.length > 0) {
    // reference_game is the title the lookup matched, so this reads
    // "not ELDEN RING" rather than "excluding 1 title".
    chips.push({
      key: "excluded_games",
      label: `not ${parsed.reference_game ?? "referenced game"}`,
      remove: (p) => ({ ...p, excluded_app_ids: [] }),
    });
  }

  return chips;
}
