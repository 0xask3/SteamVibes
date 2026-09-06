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
  before — UPDATE as well as INSERT. It is 1020MB over 130,651 vectors, and a
  full-table UPDATE forces a new index entry per row, so a backfill or a
  `--reload` with the index present takes many minutes instead of seconds.
  `alembic downgrade 0006` drops it, `upgrade head` rebuilds it.
  Size is a step function of dimension, not a ratio: at 1024 dims an element is
  ~4.4KB, so only one fits an 8KB page and the index doubled rather than growing
  33% (measured 1.000 pages per element). See NOTES.md 2026-09-04.
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
  The rule inverts for the Vite dev server, which binds `::1` *only*: open
  `http://localhost:5173`, because `http://127.0.0.1:5173` refuses the
  connection outright. Both are in the API's CORS origin list for that reason.
- The embedding client holds one long-lived `httpx.Client` and calls
  `/api/embed` with a batch. One-text-per-request is 30x slower.
- `uv run alembic check` belongs beside mypy and ruff. It is the only one of
  the three that compares the code against the real database. It caught that
  `ix_games_embedding_hnsw` existed in Postgres but not in `models.py`, which
  meant the next `--autogenerate` would have proposed dropping a 1GB index.
- Array columns use `sqlalchemy.dialects.postgresql.ARRAY`, never
  `sqlalchemy.ARRAY`. Only the dialect type implements `.contains()` (`@>`) and
  `.overlap()` (`&&`); the base type raises at runtime and mypy does not catch
  it. Same DDL, so switching needs no migration.
- FastAPI endpoints are `def`, never `async def`. `search()` and `parse_query()`
  are synchronous and block on network I/O (Ollama, then Postgres); declared
  `async` they run on the event loop and serialise every request behind the
  slowest one. As plain `def` they go to FastAPI's threadpool, which is safe
  here because `session_scope()` builds a fresh Session per call and
  `httpx.Client` is thread-safe. Verified: `/api/health` answers in 30ms while
  a 1.3s parse is in flight.
- A bare `@property` does not serialise. Anything that must cross the API
  boundary needs `@computed_field` above it — `under_delivered` was invisible
  in JSON while working fine from Python, which would have silently dropped
  the "filters starved the index" warning at exactly the point a user sees it.
- `app/title_lookup.py` recognises a game named in the query and borrows its
  tags — the embedding cannot, "elden ring" is 133rd of 452 tags away from
  `Souls-like`. Tags are appended to `semantic_query`, never added to
  `required_tags`: requiring all six of a game's tags returns almost nothing.
- The title match is a PREFIX match — word windows from the query against the
  start of a name — not a substring one. Steam names are longer than what
  anyone types (`Call of Duty®`, `DARK SOULS™: Prepare To Die Edition`), so
  asking whether the name sits inside the query fails for exactly the games
  people reference. No `pg_trgm` and no migration; failures.md #21 wrongly
  said otherwise and #23 corrects it. Exclusion is a prefix too, so "excluding
  call of duty" drops all 24 entries rather than one — which also means it
  drops sequels and spinoffs.
  `TITLE_MATCH_MIN_REVIEWS=50000` is load-bearing, not tuning — common words
  are real titles (`Nothing` has 9,260 reviews, plus `Something`, `Dollar`,
  `SELF`, `Beat`), so a lower floor makes "nothing scary" match a horror game.
- `reference_game`, `excluded_app_ids` and `min_reviews` are stripped from the
  schema handed to Ollama, in `_llm_schema()`. All three are derived in code.
  Left in, the model invents plausible app_ids, and a wrong one silently
  removes a real result.
- The prompt is FULL, and position within it does not help. Three separate
  edits have now each silently destroyed a working filter — twice from the tag
  block, once from the scalar block (failures.md #13, #22). New intents go in
  code as a regex over the query text, the way `wants_popular()` and
  `wants_reference_excluded()` do. The cost is that unphrased variants are
  missed; the alternative has cost a filter every single time.
- Search ranking is two-stage: HNSW retrieves `RERANK_CANDIDATES` rows by pure
  cosine distance, then stage 2 reorders them with a popularity term. The blend
  can never go in the first ORDER BY - pgvector only accelerates
  `ORDER BY embedding <=> :v`, and any composite expression there silently drops
  to an exact scan. `RERANK_CANDIDATES` must not exceed `HNSW_EF_SEARCH`;
  config.py raises if it does, because stage 1 coming up short is silent.
- `HNSW_EF_SEARCH=200`, not pgvector's default of 40. The default cost 3.3
  recall points at threshold 10 (15.0% against the exact scan's 18.3%) for 8ms.
- Ranking weight is chosen by the defect it fixes, NOT by recall@10. All 37
  ground-truth games in `queries.yaml` have >=11,267 reviews and none under
  1,000, so recall rises monotonically with the popularity weight until the long
  tail is gone - at the eval optimum only 1% of returned results have under
  1,000 reviews, against 79% unweighted. `rrf w=0.20` is the smallest setting
  that fixes the known failure while keeping 70% of results in the tail. Before
  raising it, add long-tail labelled queries. See failures.md #26.
- Commit per feature, not per session.
- When something breaks, three lines in `NOTES.md`: what broke, what I
  tried, what fixed it.

## Current state

Weekend 1 COMPLETE. Weekend 2 COMPLETE. Weekend 3: 1024-dim re-embed and the
three-model comparison done. Migrations 0001-0007.

API: `app/main.py` serves `POST /api/search`, `GET /api/game/{app_id}` and
`GET /api/health` over the same `search()` the CLI uses — no second
implementation. `SearchRequest` carries an optional `parsed`: when present its
filters are used verbatim and no chat model runs, which is the editable-chip
path. Measured 1.347s (parse 1247ms of it) versus 0.095s when the chips supply
the filters, so re-parsing on a chip edit would be both wrong — it re-derives
the chip just removed — and 14x slower. `app/games.py` holds the detail
lookup, kept out of `app/search.py` so the ranking path stays undiluted.

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
`Multi-player` alone — 744 of 22,127 co-op games lack that category. It
excludes `Remote Play Together`, which is a streaming feature rather than a
mode: 637 Single-player-only games qualified for it. See failures.md #18.
Unknown tags are reported rather than silently returning nothing.

`OLLAMA_KEEP_ALIVE=30m`: Ollama's 5m default evicts the model, and an idle CLI
then spends ~18s reloading it to do ~20ms of work. See NOTES.md 2026-08-29.

Environment: Python 3.14.7 via uv; Ollama native on the host serving
`qwen3-embedding:0.6b` at ~82 embeddings/sec at batch 128; Postgres 16.15 +
pgvector 0.8.6 in Docker (`docker compose up -d db`). Rate is per model and
varies 2x — see the table in `ingest/embed_all.py`, and read it off a finished
run, never a sample.

Data: `data/games.json` — 138,964 games. Use the JSON, not the CSV: the CSV is
missing 13,109 games, has a 39-vs-40 column header offset, drops tag vote
counts, and has no `short_description` column at all.

Built: `app/config.py`, `app/db.py`, `app/models.py`, and migration `0001`
(games, game_tags, game_genres, game_categories), plus `0002` adding
`discount_pct` and generated `list_price_usd`. Verified: upgrade, downgrade
to base, upgrade again; `vector(768)` (widened to 1024 by `0006`) and the
generated `total_reviews` column confirmed in `\d games`; a two-row cosine
ranking returns the sane order; `ON DELETE CASCADE` confirmed. mypy and ruff
clean.

Loaded: all four tables populated in 2.9 min — 138,964 games, 1,180,522 tags,
611,783 categories, 376,325 genres; 130,651 rows carry `embed_text`. Verified
idempotent (re-run skips) and `--reload` upserts without duplicating. Source
has 1,320 duplicate category entries, deduped at load.

Embedded: all 130,651 rows carry a `qwen3-embedding:0.6b` vector at 1024 dims,
at ~82/sec. Migration `0003` built the HNSW index (`vector_cosine_ops`, m=16,
ef_construction=64) after embedding; `0006` widened the column and dropped the
index, `0007` rebuilds it at 1024. Verified by query plan: `Index Scan using
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
2. DISPROVED, do not attempt. The claim was that `embed_text` weights titles
   over tags and that name-last or repeated tags would fix it. Measured on real
   rows: three recipes x with/without nomic prefixes, all six scored 1/5
   relevant in the top 5, rankings unmoved. Deleting `{name}` from the f-string
   does not remove the name from the text — 32,201 of 130,633 descriptions
   (25%) start with the game's own name. See failures.md #19 before reopening
   this.

Weekend 2 COMPLETE: parser, API and React with editable filter chips all done.

Weekend 3 in progress. The embedding model is `qwen3-embedding:0.6b`, picked by
measurement over `snowflake-arctic-embed2` and `bge-m3` — **not** bge-m3, which
this file used to name as the target on the strength of its MIRACL score and
which came last or joint-last at four of five review thresholds. Query/document
prefixes are keyed on the model name in `app/embedding.py`, which also lands the
nomic prefixes failures.md #19 wanted.

The choice is threshold-dependent and provisional: qwen3 wins below ~1,000
reviews, arctic wins above it by 19 points. Ranking now has the popularity term
that #24 asked for, so the re-measure it was waiting on is due - one `--reload`
and one sweep, ~30 min.

Ranking: `rrf w=0.20` over a 200-candidate pool. recall@10 is 25.0 / 26.7 /
30.0 / 41.1 / 42.2% across the five thresholds, from 18.3 / 22.8 / 26.7 / 38.3 /
38.9. Query time 44-85ms. `run_eval`'s latency figure includes the embedding
call, so it is not a query measurement - Ollama swung 94-834ms after a host
restart and made ranking look 20x slower than it is.

Still worth carrying: trigram title matching for franchise names with
™/edition suffixes (failures.md #21).
`SearchResponse` carries `parsed`; posting it back with a filter removed is the
chip interaction, and it costs no LLM call.

(update this at the end of every session)
