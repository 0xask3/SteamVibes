# Steam Vibe Search

## What this is
Semantic search over ~120k Steam games. Natural-language query in, ranked
games out. Hybrid: structured SQL filters + pgvector similarity.

The user describes the *feeling* of a game they want, in English or German.
A local LLM parses that into structured filters plus a semantic remainder;
SQL handles the filters, pgvector handles the vibe.

Full design and weekend-by-weekend scope: `BUILD_PLAN.md`. Read it before
proposing anything structural.

## Rules
- Python 3.14, type hints on every function signature.
- Pydantic v2 for all boundaries (API in/out, LLM output parsing).
- SQLAlchemy 2.0 style (`select()`, not legacy Query).
- No LangChain, no LlamaIndex, no agent frameworks. Direct HTTP to Ollama.
- All DB schema changes go through Alembic migrations. Never edit tables
  by hand.
- Every ingest script must be idempotent and resumable — I will interrupt
  them.
- Errors from the LLM are expected, not exceptional. Parse failures must
  degrade to pure semantic search, never 500.

## Do not
- Add LangChain, LlamaIndex, or any agent framework "to simplify the LLM
  calls". Every LLM call is a direct HTTP request I can read.
- Wrap the search in an agent loop.
- Write `try: except: pass` around LLM parsing. I want the fallback *and*
  the log line.
- Create or alter tables without an Alembic migration.
- Add a `requirements.txt` alongside `pyproject.toml`.

(Append to this list the first time each new one happens.)

## Review discipline
Two categories, and I'll say which one we're in at the top of each session:
- **Ingest scripts, eval harness, throwaway CLI** — if it runs, it's fine.
- **Schema, migrations, query parser, search ranking** — I read every line.
  Go through plan mode first. A silently wrong `ORDER BY` produces
  plausible results forever.

## Commands
- `docker compose up -d db ollama` — start deps
- `cd backend && uv run uvicorn app.main:app --reload`
- `cd backend && uv run python -m eval.run_eval`
- `cd frontend && npm run dev`

## Conventions
- Embedded text per game is `{name}. {short_description} Tags: {top 15 tags
  by votes}.` The tags carry most of the signal — don't drop them.
- Baseline search filters out games with `positive_reviews +
  negative_reviews <= 10`. The threshold is a config value, never inlined
  in a query — Weekend 3 measures recall at several values. Deviates from
  BUILD_PLAN.md, which said 50: the 11-50 band is 24,762 real long-tail
  indie games, 100% of which have tags and a description.
- Embed every game that has a short_description or tags — 130,651 of
  138,964. The 8,313 with neither are Playtest/Closed Beta entries, not
  games. Embedding is cheap (~35 min), so scope is generous and the
  quality gate lives at query time, not ingest time.
- The HNSW index on `games.embedding` is built *after* bulk insert, never
  before.
- The query parser is grounded on the real tag vocabulary (top ~200 tags
  passed into the prompt). Invented tags get fuzzy-matched, then dropped —
  never returned as a filter that yields zero results.
- Never use `localhost` in a connection string on Windows — it resolves to
  IPv6 `::1` first and costs ~2.1s per new connection. Always `127.0.0.1`.
- The embedding client holds one long-lived `httpx.Client` and calls
  `/api/embed` with a batch. One-text-per-request is 30x slower.
- Commit per feature, not per session.
- When something breaks, three lines in `NOTES.md`: what broke, what I
  tried, what fixed it.

## Current state
Weekend 1, step 3 of 6 done.

Environment: Python 3.14.7 via uv; Ollama native on the host serving
`nomic-embed-text` at ~62 embeddings/sec batched; Postgres 16.15 + pgvector
0.8.6 in Docker (`docker compose up -d db`).

Data: `data/games.json` — 138,964 games. Use the JSON, not the CSV: the CSV is
missing 13,109 games, has a 39-vs-40 column header offset, drops tag vote
counts, and has no `short_description` column at all.

Built: `app/config.py`, `app/db.py`, `app/models.py`, and migration `0001`
(games, game_tags, game_genres, game_categories). Verified: upgrade, downgrade
to base, upgrade again; `vector(768)` and the generated `total_reviews` column
confirmed in `\d games`; a two-row cosine ranking returns the sane order;
`ON DELETE CASCADE` confirmed. mypy and ruff clean.

Next: `ingest/load_games.py` — stream `games.json` into the four tables,
idempotent and resumable. Then `ingest/embed_all.py`, then migration `0002`
adding the HNSW index (only after embedding), then the CLI search script.

(update this at the end of every session)
