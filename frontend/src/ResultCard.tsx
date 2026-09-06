import type { SearchResult } from "./types";

/** "Free", "$9.99", or "—" when the source had no price at all. */
function price(result: SearchResult): string {
  if (result.is_free) return "Free";
  // Already a formatted decimal string from the API; do not coerce to Number.
  return result.list_price_usd ? `$${result.list_price_usd}` : "—";
}

export function ResultCard({ result }: { result: SearchResult }) {
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

      {result.short_description && <p className="blurb">{result.short_description}</p>}
    </article>
  );
}
