/**
 * Turns a ParsedQuery into removable chips.
 *
 * This is the TypeScript half of ParsedQuery.describe() in
 * backend/app/schemas.py. The labels are deliberately the same wording as the
 * CLI prints, so what you see in the browser and what you see in a terminal
 * are the same sentence.
 *
 * Each chip carries its own `remove`, which returns a NEW ParsedQuery rather
 * than mutating - React needs a fresh object to re-render, and the result is
 * posted straight back to the API as the chip-edit request.
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

  return chips;
}
