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
- Refreshing `data/games.json`: follow `backend/ingest/README.md`. In short —
  `alembic upgrade head`, `load_games --reload`, `embed_all`, then the two
  SQL checks. `--reload` is required or existing games are skipped.

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
  games. Embedding is cheap (~12 min), so scope is generous and the
  quality gate lives at query time, not ingest time.
- Search filters price on `list_price_usd`, never `price_usd`. The Kaggle
  snapshot caught a Steam sale — 37.7% of paid games are discounted, so
  `price_usd` is a sale price. `list_price_usd` is generated from it and
  `discount_pct`, and is a cent low by construction. See NOTES.md 2026-08-26.
- The HNSW index on `games.embedding` is built *after* any bulk write, never
  before — UPDATE as well as INSERT. It is 510MB over 130,651 vectors, and a
  full-table UPDATE forces a new index entry per row, so a backfill or a
  `--reload` with the index present takes many minutes instead of seconds.
  `alembic downgrade 0004` drops it, `upgrade head` rebuilds it.
- Tag strings must match exactly: the real tags are `Co-op` and `Base-Building`,
  not `Base Building`. A wrong string returns zero rows with no error, which is
  why the parser is grounded on the real vocabulary.
- The query parser is grounded on the real tag vocabulary — all 452 tags, not
  BUILD_PLAN's "top ~200". They cost ~1,400 tokens, and passing every one
  removes a whole failure class: a tag that exists but was never shown.
  Invented tags get fuzzy-matched, then dropped — never returned as a filter
  that yields zero results.
- Field order in `ParsedQuery` is load-bearing, not cosmetic. Ollama's `format`
  constrains generation, so fields are emitted in declaration order and an
  earlier one cannot be revised. `semantic_query` must stay LAST: it is the
  query with every extracted constraint removed, so it depends on all the
  others. Declared first, both models returned the original sentence unstripped.
- The parser prompt's layout is tuned and the two blocks compete. Whatever sits
  nearest the query wins: vocabulary at the bottom and the scalar rules lose
  price/platform/year; vocabulary at the top and tag extraction collapses.
  Current layout — rules, worked example, then vocabulary last — passes both.
  Never edit that prompt without re-running `eval/compare_parsers.py`; it reads
  fine either way and fails silently.
- `CHAT_NUM_CTX` must comfortably hold the vocabulary prompt (~1,800 tokens).
  An overflow truncates silently from the end, dropping tags off the list —
  the exact failure passing all 452 was meant to remove.
- Chat models are thinking models now: send `think: false` or pay seconds of
  latency per parse. It is `bool | None` and omitted when `None`, because
  Ollama 400s on `think` for models that predate it. Ollama issue #14645 —
  `format` silently ignored when thinking is disabled — is fixed as of 0.33.2,
  verified by curl before the parser was trusted. Re-check after an upgrade.
- In Git Bash, absolute paths passed to `docker compose exec` get rewritten by
  MSYS (`/dev/shm` becomes `C:/Program Files/Git/dev/shm`). Escape with a
  leading double slash: `//dev/shm`, or prefix `MSYS_NO_PATHCONV=1`.
- Never use `localhost` in a connection string on Windows — it resolves to
  IPv6 `::1` first and costs ~2.1s per new connection. Always `127.0.0.1`.
- The embedding client holds one long-lived `httpx.Client` and calls
  `/api/embed` with a batch. One-text-per-request is 30x slower.
- `uv run alembic check` belongs beside mypy and ruff. It is the only one of
  the three that compares the code against the real database. It caught that
  `ix_games_embedding_hnsw` existed in Postgres but not in `models.py`, which
  meant the next `--autogenerate` would have proposed dropping a 510MB index.
- Array columns use `sqlalchemy.dialects.postgresql.ARRAY`, never
  `sqlalchemy.ARRAY`. Only the dialect type implements `.contains()` (`@>`) and
  `.overlap()` (`&&`); the base type raises at runtime and mypy does not catch
  it. Same DDL, so switching needs no migration.
- Commit per feature, not per session.
- When something breaks, three lines in `NOTES.md`: what broke, what I
  tried, what fixed it.

## Current state

Weekend 1 COMPLETE. Weekend 2: schema, structured filtering and the query
parser done; FastAPI and React next. Migrations 0001-0005.

Parser: `app/query_parser.py` turns natural language into the same
`ParsedQuery` the CLI flags build — one code path, not two. `app/llm.py` is the
Ollama chat client (`/api/chat`, `format` = the Pydantic JSON schema,
`temperature=0`, `think=false`). `CHAT_MODEL=qwen3.5:9b`, chosen against
`qwen3.5:4b` on evidence: 4b is 0.14s faster but returns no filters at all on
2 of 10 queries, one of them German. Warm parse ~0.73s. Failures never raise —
any error degrades to `ParsedQuery(semantic_query=text)` with a WARNING, which
is exactly the pre-parser behaviour.

Two rounds of prompt iteration, all of it driven by
`eval/compare_parsers.py`; round 1 disagreed on 10 of 10 queries at
temperature 0, which is a prompt problem, not model variance. Fixes and the two
still-open defects are `eval/failures.md` #12-17.

Filters: `app/search.py` takes a `ParsedQuery` (`app/schemas.py`) and applies
price, platform, tag, year, age and multiplayer in SQL before pgvector ranks
the survivors. Driven by CLI flags today; the parser fills the same object, so
there is one code path, not two. `--platform linux --max-price 20
--multiplayer` filters 130,651 rows to 1,867 and returns in ~417ms.
`multiplayer` reads `game_categories` using the full co-op set, not
`Multi-player` alone — 744 of 22,127 co-op games lack that category.
Unknown tags are reported rather than silently returning nothing.

`OLLAMA_KEEP_ALIVE=30m`: Ollama's 5m default evicts the model, and an idle CLI
then spends ~18s reloading it to do ~20ms of work. See NOTES.md 2026-08-29.

Environment: Python 3.14.7 via uv; Ollama native on the host serving
`nomic-embed-text` at ~183 embeddings/sec at batch 64; Postgres 16.15 + pgvector
0.8.6 in Docker (`docker compose up -d db`).

Data: `data/games.json` — 138,964 games. Use the JSON, not the CSV: the CSV is
missing 13,109 games, has a 39-vs-40 column header offset, drops tag vote
counts, and has no `short_description` column at all.

Built: `app/config.py`, `app/db.py`, `app/models.py`, and migration `0001`
(games, game_tags, game_genres, game_categories), plus `0002` adding
`discount_pct` and generated `list_price_usd`. Verified: upgrade, downgrade
to base, upgrade again; `vector(768)` and the generated `total_reviews` column
confirmed in `\d games`; a two-row cosine ranking returns the sane order;
`ON DELETE CASCADE` confirmed. mypy and ruff clean.

Loaded: all four tables populated in 2.9 min — 138,964 games, 1,180,522 tags,
611,783 categories, 376,325 genres; 130,651 rows carry `embed_text`. Verified
idempotent (re-run skips) and `--reload` upserts without duplicating. Source
has 1,320 duplicate category entries, deduped at load.

Embedded: all 130,651 rows carry a `nomic-embed-text` vector, at ~183/sec.
Migration `0003` built the HNSW index (`vector_cosine_ops`, m=16,
ef_construction=64) after embedding. Verified by query plan: `Index Scan using
ix_games_embedding_hnsw`, 3.4ms for a top-10 over 130,651 vectors. The db
service needs `shm_size: 4gb` or the parallel build fails — see NOTES.md.

Search: `app/search.py` holds the ranking (reused by Weekend 2's API, not
rewritten), `app/embedding.py` the shared Ollama client, `app/schemas.py` the
Pydantic result types, `search.py` the CLI. Verified against an exhaustive scan
as ground truth.

`hnsw.iterative_scan = strict_order` is set per transaction in `app/search.py`
and is NOT optional: without it a selective filter silently returns fewer rows
than requested (measured 4 of 10 at `--threshold 5000`). It costs latency —
~150ms at threshold 10, ~1450ms at 5000. Watch this when Weekend 2 stacks
filters.

Failure modes: `backend/eval/failures.md`, 11 documented with mechanisms. That
file is the raw material for `eval/queries.yaml` and for the README's "what
does not work" section.

## Carry into Weekend 2 (both cheap, both found by testing)
1. DONE. Migration `0004` added `required_age` and `games.tags text[]` with a
   GIN index, backfilled from `game_tags`; `0005` rebuilds HNSW afterwards.
   `0004` drops HNSW first — see the convention above. Only 1,321 games have
   `required_age > 0`, so age filtering must also exclude mature tags.
2. `embed_text` weights titles over tags — `{name}. {short_description} Tags:
   ...` puts a short name first, so *EasyPianoGame* (tagged `Difficult`) ranks
   for "easy relaxing game". Try name-last or repeated tags. One f-string plus
   ~12 min re-embedding, and it earns a row in the Weekend 3 results table.

Next: Weekend 2 — FastAPI (`app/main.py`) and React with editable filter chips.
`SearchResponse` already carries `parsed`, which is what the chips render, so
the API is mostly wiring.

(update this at the end of every session)
