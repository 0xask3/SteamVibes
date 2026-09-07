import type { SearchResult, VerifiedExplanation } from "./types";

/** "Free", "$9.99", or "—" when the source had no price at all. */
function price(result: SearchResult): string {
  if (result.is_free) return "Free";
  // Already a formatted decimal string from the API; do not coerce to Number.
  return result.list_price_usd ? `$${result.list_price_usd}` : "—";
}

export function ResultCard({
  result,
  explanation,
}: {
  result: SearchResult;
  explanation?: VerifiedExplanation;
}) {
  return (
    <article className="card">
      <header className="card-head">
        <a
          className="card-title"
          href={`https://store.steampowered.com/app/${result.app_id}/`}
          target="_blank"
          rel="noreferrer"
        >
          {result.name}
        </a>
        {/* Similarity, not a star rating. Once a popularity term is ordering
            the list these numbers stop descending, so say when that is why
            rather than leaving it looking like a sorting bug. */}
        <span
          className="score"
          title={
            result.rank_score === result.score
              ? "cosine similarity"
              : `cosine similarity — ordered by ${result.rank_score.toFixed(
                  4,
                )}, which also weights review count`
          }
        >
          {result.score.toFixed(3)}
        </span>
      </header>

      <div className="facts">
        <span className="price">{price(result)}</span>
        <span>{result.total_reviews.toLocaleString()} reviews</span>
        {result.positive_ratio !== null && (
          <span>{Math.round(result.positive_ratio * 100)}% positive</span>
        )}
        {result.platforms.length > 0 && <span>{result.platforms.join(" / ")}</span>}
      </div>

      {result.tags.length > 0 && (
        <div className="tags">
          {result.tags.map((tag) => (
            <span className="tag" key={tag}>
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* Visually distinct when it is NOT model-written. `grounded: false`
          means an explanation was generated and then thrown away for citing a
          tag this game does not have, and the line below is a deterministic
          fallback built from the real tags. Showing it identically would pass
          off a canned sentence as an explanation, which is the exact failure
          the verification exists to catch - so the class differs and the
          tooltip says which it is. */}
      {explanation && (
        <p
          className={explanation.grounded ? "why" : "why why-fallback"}
          title={
            explanation.grounded
              ? "Written by the local model, and every tag it cites was checked against this game's record"
              : `The model's explanation was discarded (${explanation.discard_reason}). This line is built from the game's own tags.`
          }
        >
          {explanation.why}
        </p>
      )}

      {result.short_description && <p className="blurb">{result.short_description}</p>}
    </article>
  );
}
