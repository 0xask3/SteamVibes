import { useEffect, useState } from "react";

import { explain, search } from "./api";
import { chipsFor } from "./chips";
import { ResultCard } from "./ResultCard";
import type {
  ParsedQuery,
  SearchResponse,
  VerifiedExplanation,
} from "./types";
import "./App.css";

const EXAMPLES = [
  "co-op base builder under 20 dollars that runs on linux",
  "cozy farming game with fishing",
  "cheap relaxing puzzle games, nothing scary",
  "gemütliches Aufbauspiel für zwei",
];

export default function App() {
  const [query, setQuery] = useState("");
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [slow, setSlow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Keyed by app_id and filled in AFTER results render. A second request, so
  // the list is not held back by an LLM call for text nobody has scrolled to.
  const [why, setWhy] = useState<Record<number, VerifiedExplanation>>({});

  /**
   * A warm search is ~0.8s. A cold one is ~22s, because Ollama has to page
   * 6.6GB of chat model into VRAM - it evicts after 30 minutes idle. Without
   * this the user just watches a spinner and reasonably concludes it broke.
   */
  useEffect(() => {
    if (!loading) {
      setSlow(false);
      return;
    }
    const timer = setTimeout(() => setSlow(true), 2500);
    return () => clearTimeout(timer);
  }, [loading]);

  /**
   * One entry point for both paths. Passing `parsed` skips the chat model
   * entirely - that is the chip edit, and it is ~14x faster than re-parsing.
   * Re-parsing would also re-derive whichever chip was just removed.
   */
  async function runSearch(text: string, parsed?: ParsedQuery) {
    if (!text.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const result = await search({ query: text, parsed });
      setResponse(result);
      // Cleared before the new ones arrive, or the previous search's
      // explanations sit under the new search's games for a second.
      setWhy({});
      if (result.results.length > 0) {
        // Deliberately not awaited into the loading state: results are already
        // on screen and useful. explain() swallows its own failures.
        void explain({
          query: result.parsed.semantic_query,
          app_ids: result.results.map((r) => r.app_id),
          wanted_tags: result.parsed.required_tags,
        }).then((payload) => {
          if (!payload) return;
          setWhy(Object.fromEntries(payload.explanations.map((e) => [e.app_id, e])));
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setLoading(false);
    }
  }

  const chips = response ? chipsFor(response.parsed) : [];

  return (
    <div className="page">
      <header className="masthead">
        <h1>Steam Vibe Search</h1>
        <p>
          Describe how a game should <em>feel</em>, in English or German. Hard
          constraints become database filters; the rest becomes the vibe.
        </p>
      </header>

      <form
        className="search"
        onSubmit={(event) => {
          event.preventDefault();
          void runSearch(query);
        }}
      >
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="co-op base builder under 20 dollars that runs on linux"
          aria-label="Search query"
          autoFocus
        />
        <button type="submit" disabled={loading || !query.trim()}>
          {loading ? "Searching…" : "Search"}
        </button>
      </form>

      {slow && (
        <p className="hint">
          Loading the language model into VRAM — the first search after an idle
          period takes around 20 seconds. Subsequent ones are under a second.
        </p>
      )}

      {!response && !loading && (
        <div className="examples">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              className="example"
              onClick={() => {
                setQuery(example);
                void runSearch(example);
              }}
            >
              {example}
            </button>
          ))}
        </div>
      )}

      {error && <p className="error">{error}</p>}

      {response && (
        <>
          <section className="interpretation">
            <div className="row">
              <span className="row-label">Vibe</span>
              {/* What actually gets embedded, after constraints are stripped. */}
              <code className="semantic">{response.parsed.semantic_query}</code>
            </div>

            <div className="row">
              <span className="row-label">Filters</span>
              {chips.length === 0 ? (
                <span className="muted">none — pure semantic search</span>
              ) : (
                <div className="chips">
                  {chips.map((chip) => (
                    <button
                      key={chip.key}
                      className="chip"
                      title="Remove this filter"
                      onClick={() =>
                        void runSearch(query, chip.remove(response.parsed))
                      }
                    >
                      {chip.label}
                      <span className="x">×</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </section>

          {response.unknown_tags.length > 0 && (
            <p className="warn">
              Unknown tag(s): {response.unknown_tags.join(", ")}. These match
              nothing, so results will be empty.
            </p>
          )}

          {response.under_delivered && (
            <p className="warn">
              Asked for {response.requested}, got {response.returned}. The
              filters are selective enough to starve the index — try removing
              one.
            </p>
          )}

          <div className="results">
            {response.results.map((result) => (
              <ResultCard
                key={result.app_id}
                result={result}
                explanation={why[result.app_id]}
              />
            ))}
          </div>

          {response.results.length === 0 && (
            <p className="muted">No results. Try removing a filter.</p>
          )}

          <footer className="timings">
            {response.returned} results · embed {response.embed_ms.toFixed(0)}ms ·
            query {response.query_ms.toFixed(0)}ms ·{" "}
            {response.parse_ms === null
              ? "no parse (filters supplied)"
              : `parse ${response.parse_ms.toFixed(0)}ms`}
          </footer>
        </>
      )}
    </div>
  );
}
