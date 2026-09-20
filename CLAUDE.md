# Steam Vibe Search

## What this is
Semantic search over ~139k Steam games. The user describes the *feeling* of a
game they want, in English or German; a local LLM parses that into structured
filters plus a semantic remainder, SQL handles the filters, pgvector handles
the vibe, and a cross-encoder reorders what comes back.

The record of what was tried and what broke lives in `backend/eval/failures.md`
(42 numbered entries) and `NOTES.md` (dated, what-broke/what-fixed-it). Both
are cited by number throughout this file rather than retold.

## Rules
- Python 3.14, type hints on every function signature.
- Pydantic v2 for all boundaries (API in/out, LLM output parsing).
- SQLAlchemy 2.0 style (`select()`, not legacy Query).
- No LangChain, no LlamaIndex, no agent frameworks. Direct HTTP to Ollama.
- All schema changes go through Alembic. Never edit tables by hand.
- Every ingest script must be idempotent and resumable - I will interrupt them.
- Errors from the LLM are expected, not exceptional. Parse failures degrade to
  pure semantic search, never a 500.

## Do not
- Add LangChain, LlamaIndex or any agent framework "to simplify the LLM calls".
- Wrap the search in an agent loop.
- Write `try: except: pass` around LLM parsing. I want the fallback *and* the
  log line.
- Create or alter tables without a migration.
- Add a `requirements.txt` alongside `pyproject.toml`.

## Review discipline
- **Ingest scripts, eval harness, throwaway CLI** - if it runs, it's fine.
- **Schema, migrations, query parser, search ranking** - I read every line. Go
  through plan mode first. A silently wrong `ORDER BY` produces plausible
  results forever.

## Commands
- `docker compose up -d db ollama` - start deps
- `cd backend && uv run uvicorn app.main:app --reload`
- `cd backend && uv run python -m eval.run_eval` - recall, for RETRIEVAL and
  RANKING changes. Refuses to print a table if any query fell back from the
  reranker: a part-baseline run is a different measurement wearing this model's
  label.
- `... -m eval.run_eval --queries eval/queries_de.yaml` - the same 118 targets
  asked in German. A DIFFERENT SET is a different measurement.
- `... -m eval.run_lang_eval` - EN vs DE over 91 matched pairs. The only EN/DE
  number here not confounded by tier mix or target choice.
- `... -m eval.run_parse_eval` - for PARSER changes. Recall cannot referee
  those; see the Eval section.
- `... -m eval.run_explain_eval` - explanation hallucination rate.
  `--self-test` FIRST, and after any verifier change.
- `... -m eval.compare_runs a.json b.json` - paired comparison of two
  `run_eval --dump` files, for config-vs-config changes.
- `... -m eval.paired` - self-test the significance tests. Run before believing
  any comparison; both are TWO-SIDED.
- `cd frontend && npm run dev`
- `cd frontend && npx tsc --noEmit && npm run lint` - both clean; nothing in CI
  runs oxlint.
- `uv run alembic check` belongs beside ruff and mypy: it is the only one that
  compares the code against the real database.
- Refreshing `data/games.json`: see `backend/ingest/README.md`. In short -
  `alembic upgrade head`, `alembic downgrade 0006`, `load_games --reload`,
  `embed_all`, `alembic upgrade head`, then the two SQL checks. `--reload` is
  required or existing games are skipped; `downgrade 0006` is required or
  `embed_all` refuses to start. Never go below 0006 - that discards every
  vector.

## Conventions

### Data and schema
- Embedded text is `{name}. {short_description} Tags: {top 15 tags by votes}.`
  The tags carry most of the signal - don't drop them.
- Search filters price on `list_price_usd`, never `price_usd`: the snapshot
  caught a Steam sale, so 37.7% of paid games are discounted. NOTES.md
  2026-08-26.
- Baseline search hides games with `positive + negative <= REVIEW_THRESHOLD`.
  A config value, never inlined in a query.
- Embed every game with a short_description or tags - 130,651 of 138,964. The
  rest are Playtest/Closed Beta entries. The quality gate lives at query time,
  not ingest time.
- Tag strings must match EXACTLY: the real tags are `Co-op` and
  `Base-Building`. A wrong string returns zero rows with no error, which is why
  the parser is grounded on the real vocabulary.
- `required_tags` filters with `&&` (any-of), NOT `@>` (all-of): all-of scored
  55.4% against any-of's 60.0% and returned zero rows on three-tag queries. The
  tag filter is a coarse recall gate; the vector discriminates. `@>` is still
  right for `excluded_tags`. failures.md #33.
- Array columns use `sqlalchemy.dialects.postgresql.ARRAY`, never
  `sqlalchemy.ARRAY` - only the dialect type implements `.contains()` and
  `.overlap()`, and mypy does not catch it.
- The HNSW index is built AFTER any bulk write, never before - UPDATE as well
  as INSERT. It is 1020MB over 130,651 vectors, and size is a step function of
  dimension, not a ratio. NOTES.md 2026-09-04.
- The db service needs `shm_size: 4gb` or the parallel build fails.

### Parser
- The parser is grounded on all 452 real tags, not a top-200 subset: ~1,400
  tokens, and it removes a whole failure class - a tag that exists but was
  never shown. Invented tags are fuzzy-matched, then dropped.
- FIELD ORDER IN `ParsedQuery` IS LOAD-BEARING. `format` constrains generation,
  so fields are emitted in declaration order and an earlier one cannot be
  revised. `semantic_query` must stay LAST. That only holds because
  `_REQUIRED_FIELDS` exists.
- An optional field in a constrained-decoding schema is an invitation to OMIT
  it. A Pydantic-generated schema is almost entirely optional by accident, and
  the model dropped `required_tags` on 64% of parses. Every scalar belongs in
  `_REQUIRED_FIELDS`; the tag ARRAYS deliberately do not - forcing those costs
  15.9 points of tail recall. failures.md #33.
- Making a field REQUIRED can make the model INVENT a value, which is a
  different bug from the one you fixed. `_drop_invented_platforms()` guards
  that, and only fires when the query names no OS. Check every field you made
  required, arrays included.
- The prompt is FULL. Three separate edits have each silently destroyed a
  working filter (#13, #22, #32). New intents go in code as a regex, the way
  `wants_popular()` and `wants_singleplayer()` do.
- A code rule is also the NET under an intent the prompt already holds:
  `multiplayer` is in the prompt and still returned null when four clauses
  competed. Such a regex FILLS, never overrides, and needs a negation guard -
  "no multiplayer" contains "multiplayer". failures.md #32.
- A constraint converted into a FILTER must stop steering the vector.
  `_strip_popular()` removes what `min_reviews` consumed, reusing the same
  pattern so trigger and removal cannot drift. Any new code rule answers both
  halves: what filter it sets, and what it removes from the text.
  `wants_reference_excluded` still leaks this way. failures.md #34.
- German adjectives inflect: every adjective in a regex needs `\w*`, and check
  the near-miss (`beliebig` must not match `beliebt\w*`).
- The prompt's LAYOUT is tuned and the two blocks compete - whatever sits
  nearest the query wins. Never edit it without re-running
  `eval/compare_parsers.py`; it reads fine either way and fails silently.
- `CHAT_NUM_CTX` must hold the vocabulary prompt (~1,800 tokens). An overflow
  truncates silently from the end.
- Chat models are thinking models now: send `think: false` or pay seconds per
  parse. It is `bool | None` because Ollama 400s on `think` for models that
  predate it. Re-check Ollama issue #14645 after an upgrade.
- `app/title_lookup.py` recognises a game named in the query and borrows its
  tags - the embedding cannot ("elden ring" is 133 tags away from `Souls-like`).
  Tags are appended to `semantic_query`, never added to `required_tags`.
- The title match is a PREFIX match, not a substring one: Steam names are
  longer than what anyone types (`Call of Duty®`). No pg_trgm, no migration.
  Exclusion is a prefix too, so it drops sequels and spinoffs. failures.md #23.
- `TITLE_MATCH_MIN_REVIEWS=50000` is load-bearing: common words are real titles
  (`Nothing` has 9,260 reviews), so a lower floor makes "nothing scary" match a
  horror game.
- A ONE-WORD title is only a candidate behind a reference CUE. Generating every
  single word instead takes false positives from 4 to 24 over the eval set.
  `MIN_NAME_LENGTH` is 5, or `Hades`/`Stray`/`Forza` stay unreachable.
  failures.md #35.
- `apply_reference` must never borrow a tag that CONTRADICTS the filters just
  extracted - the game's tags describe the GAME, not the request. Exact match
  only, or an excluded `Action` would drop `Action RPG`. failures.md #32.
- `reference_game`, `excluded_app_ids` and `min_reviews` are stripped from the
  schema handed to Ollama. Left in, the model invents plausible app_ids.

### Search and ranking
- Stage 1 HNSW retrieves `RERANK_CANDIDATES` rows by pure cosine; stage 2
  computes the rrf blend in SQL; stage 3 (`RANK_METHOD=rerank`) rescores the
  whole pool with a cross-encoder whose RANK replaces the cosine rank inside
  that same rrf sum - same `k`, same `w`, deliberately not a new formula.
- The blend can NEVER go in the first ORDER BY: pgvector only accelerates
  `ORDER BY embedding <=> :v`, and a composite expression silently drops to an
  exact scan.
- `RERANK_CANDIDATES` must not exceed `HNSW_EF_SEARCH`; config.py raises,
  because stage 1 coming up short is silent. 200 is measured: every reachable
  `specific` target is inside rank 100, and 200->500 buys only `core` targets.
- `hnsw.iterative_scan = strict_order` is NOT optional: without it a selective
  filter silently returns fewer rows than requested.
- `HNSW_EF_SEARCH=800`, and the reason is REPRODUCIBILITY before recall. At 200
  the answer depended on which graph the parallel build produced (five builds:
  66.8-68.8%). 800 is the smallest value where every build agrees on every
  query of both sets and matches 1000. Do not lower it to 600 on its 0.3-point
  higher score - that was still-approximate search helping one query.
  failures.md #42.
- `RERANK_BATCH_SIZE` is mostly speed and VRAM but NOT only that: a different
  batch shape changes the fp16 arithmetic. 16 moved 13 of 30 top-10s; 32
  reordered near-ties inside 4 of 236 while keeping every SET. Change it only
  with `compare_runs` at 0 wins and 0 losses.
- Ranking weight is chosen by the `tail` tier and the tail-cost counter-metric,
  NEVER by `core` or `specific`: their targets sit above the 93rd percentile,
  so recall there rises with the weight however much tail it deletes.
  failures.md #26.
- The reranker runs IN-PROCESS on the host GPU - the one model not behind HTTP.
  Both serving routes are dead here and both fail silently: Ollama has no
  rerank endpoint, and TEI cannot reach the GPU through Docker Desktop's WSL2
  backend. The containerised backend therefore CANNOT rerank, and compose pins
  `RANK_METHOD=rrf`. NOTES.md 2026-09-07.
- `uv add torch` on Windows installs a CPU-ONLY wheel silently; the only
  symptom is `torch.cuda.is_available() == False`. pyproject pins a NAMED index
  with `explicit = true` plus a `[tool.uv.sources]` binding.
- LOADING IS NOT SCORING. A model can load cleanly and raise on every
  `predict()`, degrading all 118 queries to the SQL ordering and printing a
  table byte-identical to the control - which reads as "no better" rather than
  "never ran". `verify_rerank_model()` scores a probe pair and rejects CONSTANT
  scores too. failures.md #36.
- A reranker's input TEMPLATE is load-bearing and its absence is silent:
  without Qwen3's chat template the model scored 8.1%. Compare SCORE SPREAD on
  a known-ordered triple before believing any recall number - a mis-invoked
  model still orders an easy triple correctly, just without conviction.
  failures.md #37.
- A RELEVANCE-ONLY cross-encoder is worse at short genre labels and recall
  cannot see it: bge drops Cities: Skylines II from rank 1 to 37 for "city
  builder" while `core` recall says it improved. Prefer a model that can be
  TOLD what relevance means. failures.md #36.
- Query relaxation widens filters when they cannot fill a page, and its
  important half is the NEVER-RELAX list: `max_required_age`, `excluded_tags`,
  `excluded_app_ids`, `multiplayer` and `platforms` are never touched. Verify
  that as BEHAVIOUR, not by reading the list.
- The relaxation loop runs on CAPPED COUNTS, never on retried searches, and
  reuses `_apply_filters` rather than restating the WHERE clauses. No model is
  in the loop, deliberately.
- `SearchResponse.parsed` means what was ACTUALLY applied, so after relaxation
  it holds the widened filters and `relaxed` carries the diff. The chips must
  show the query that RAN.
- FastAPI endpoints are `def`, never `async def`: `search()` and
  `parse_query()` block on network I/O, and declared async they would serialise
  every request behind the slowest one.
- A bare `@property` does not serialise. Anything crossing the API boundary
  needs `@computed_field`.
- `/api/explain` is a SECOND request, not part of `/api/search`. Its `query`
  MUST be `parsed.semantic_query`, never the typed text: the model sees tags
  but no platforms or prices, so "...on linux" made it deny Linux for 15 of 15
  Linux games, all passing verification.
- THE API IS SINGLE-USER UNDER LOAD. Thirty concurrent searches took 209
  SECONDS each against 2.4s served one at a time, because one GPU runs both
  models and nothing bounds how many requests pile onto it. Do not benchmark
  this API concurrently and read the result as latency.

### Explanations
- The deliverable is the DISCARD RATE, not the sentence. Grounding data comes
  from the DATABASE, never the caller; a discard is FINAL (retrying converts a
  measured failure rate into a hidden latency cost); and `grounded=False`
  travels to the UI. failures.md #38.
- A VERIFIER IS A MEASURING INSTRUMENT AND GETS MEASURED LIKE ONE. The first
  full run reported 7.3% hallucinated and three of eight audited discards were
  the checker's fault - all in the safe-looking direction, which is the one
  nobody audits.
- A prose scan over the vocabulary needs three guards, each found by audit:
  overlapping tags resolve LONGEST-FIRST; a tag in a NEGATED clause is a denial;
  and a SENTENCE-INITIAL single-word tag is grammar, not a citation.
- `Explanation`'s field order is load-bearing for `ParsedQuery`'s reason:
  `cited_tags` before `why` makes the model commit to a list and then write
  prose consistent with it.
- `run_explain_eval.py --self-test` must pass before any rate from it is
  believed: four known-bad arms plus a truthful CONTROL, because a verifier
  that rejects everything would otherwise score perfectly.

### Caching, metrics and infrastructure
- NOTHING READ FROM THE DATABASE MAY BE CACHED FOR THE LIFE OF THE PROCESS.
  `get_tag_vocabulary()` was an `lru_cache`, and the startup warmup parses a
  query, so the container cached the tags of an EMPTY database - after ingest
  it extracted no tags at all, with nothing logged. Any new DB-derived cache
  needs both answers: what if it is filled before ingest, and during it?
- `/api/stats` is a ring buffer, and its numbers are the easiest here to quote
  wrongly, so each ships with its scope: the last N REQUESTS (not a period),
  per-process, recorded at the ENDPOINT - so its p50 is NOT run_eval's median.
- P95 IS WITHHELD BELOW 21 SAMPLES, AND 21 IS EXACT: with nearest-rank the
  index only stops being the last element at n=21. Assert `p95 < max`, do not
  reason about it.
- A STAGE THAT DID NOT RUN IS OMITTED, NEVER ZEROED, and NOTHING IS EXCLUDED
  from the window - including the cold start.
- `metrics.note()` counts EVENTS, NOT REQUESTS: never divide one by `search.n`.
  `_warm_models()` parses at startup, so one search can report two failures.
- The parse fallback is counted in `app/query_parser.py` because it leaves no
  other trace - a bare `ParsedQuery` is byte-identical to a query that carried
  no constraints. `parse_call_failed` and `parse_bad_output` are split: one is
  Ollama unreachable, the other the model answering unusably.
- THE PARSER COSTS MORE THAN THE CROSS-ENCODER: 1,217ms p50 against 1,048ms,
  with embed at 31ms and SQL at 46ms. Measure the whole pipeline before
  optimising the stage that looks expensive.
- Rerank latency is sensitive to VRAM CONTENTION; a run taken after other GPU
  work is not a measurement of the reranker. Re-run before writing one down.
- The embedding client holds one long-lived `httpx.Client` and batches
  `/api/embed`. One-text-per-request is 30x slower. Never let an HTTP error
  reach the caller without its body - `raise_for_status()` drops the one line
  that explains it.
- Ollama checks the PACKED token count of several embed inputs against the
  physical batch, so an ordinary batch can be rejected while every text is
  tiny. `EMBED_NUM_BATCH=4096` raises the ceiling; `_post_batch` halves and
  retries, and its WARNING per split is load-bearing.
- `verify_corpus_model()` and `verify_corpus_complete()` catch the two silent
  corpus faults - the wrong model, and the right model over half the table.
  Both are wired into `run_eval` only; a partial corpus is a normal thing to
  search from and a fatal thing to measure from.
- `OLLAMA_KEEP_ALIVE=30m`: the 5m default evicts the model and an idle CLI then
  pays ~18s to do ~20ms of work.
- Never use `localhost` in a connection string on Windows - it resolves to IPv6
  `::1` first and costs ~2.1s per connection. The rule INVERTS for Vite, which
  binds `::1` only: open `http://localhost:5173`. Both are in the CORS list.
- In Git Bash, absolute paths passed to `docker compose exec` get rewritten by
  MSYS. Escape with a leading double slash or prefix `MSYS_NO_PATHCONV=1`.
- `docker compose up` runs db + backend + frontend. Three load-bearing details:
  the frontend publishes on host 5173 because that is what the CORS allowlist
  names; the backend reaches Ollama at `host.docker.internal` (with
  `extra_hosts: host-gateway`, which makes the file work on native Linux too);
  and `ollama` sits behind a profile so it cannot take port 11434 from the host
  install that has the GPU.
- Ingest CANNOT run inside the backend container: the build context is
  `./backend`, so `data/games.json` is not in the image, and embedding wants
  the host GPU. `docker compose up` therefore reaches a working API over an
  EMPTY database, which README documents rather than papers over.

### Measurement
- REPRODUCIBLE IS NOT DISTINGUISHABLE. The floors below measure re-running the
  SAME config; the uncertainty in a DIFFERENCE between two configs is far
  larger, because ~100 of 118 queries tie. Run a paired bootstrap and a sign
  test BEFORE writing a comparison table. failures.md #37.
- Differences below ~2.5 points at n=44, or ~1 point at n=118, are NOT results.
  Vectors ARE reproducible at a fixed `num_batch`, even across the Ollama
  0.33->0.34 upgrade; the PARALLEL index build is NOT (five builds, 66.8-68.8%
  at ef_search 200). Since 800 the build changes nothing on any of 236 queries.
  Never change embedding batch settings mid-corpus - a corpus embedded two ways
  passes both verify functions. failures.md #31 and #42.
- `run_eval --parse` has a SECOND floor: the parser is deterministic within a
  run and NOT across runs at `temperature=0` - four queries moved between two
  runs of identical code. Treat anything under ~3.5 points as noise and budget
  three runs. When a `--parse` number moves, diff the per-query lines and prove
  the change can reach the query that moved. failures.md #32, #33.
- `eval/paired.py` is TWO-SIDED. CLAUDE.md's recorded Qwen3-vs-bge `p=0.105` is
  the ONE-SIDED tail of the same 11-5 split (0.2101 two-sided); never compare
  the two numbers.
- Recall is comparable across CONFIGS on a fixed set, never across SETS. Going
  from 74 to 118 queries once invalidated every number measured on the old one.
  A SECOND QUERY SET GOES IN A SECOND FILE.
- Target OBSCURITY is what lets a tier price a ranking weight; query wording is
  not, and that was measured. New tiers come from `sample_longtail.sql` - check
  its percentile column before writing a word. failures.md #28.
- `run_eval` sets `relax_filters=False` and prints `relax: OFF`: a harness that
  widened filters whenever a query returned little would report relaxation as
  retrieval quality.
- `run_eval --dump` writes per-query scores; `compare_runs` refuses two dumps
  from different sets, or rows that do not line up.

## Current state

**Weekends 1-4 complete. The project is wound up.** Migrations 0001-0007.
Weekend 5 (discount likelihood) is SKIPPED on a checkable reason: it needed a
price collector running from day one, `price_history` exists nowhere in this
repo, and the only price data is a single snapshot taken during a sale.

- **API** (`app/main.py`): `POST /api/search`, `POST /api/explain`,
  `GET /api/game/{app_id}`, `GET /api/health`, `GET /api/stats`, over the same
  `search()` the CLI uses. `SearchRequest.parsed` is the editable-chip path -
  0.095s against 1.347s, and re-parsing would re-derive the chip just removed.
- **Parser** (`app/query_parser.py`): `CHAT_MODEL=qwen3.5:9b`, chosen against
  `qwen3.5:4b` on evidence - 4b is 0.14s faster and returns no filters at all
  on 2 of 10 queries. Warm parse ~0.73s. Failures never raise.
- **Embeddings**: `snowflake-arctic-embed2` at 1024 dims, all 130,651 rows,
  97.7/sec. Picked by measurement over `qwen3-embedding:0.6b` and `bge-m3`:
  +7.5 points overall, +15.9 `specific`, +9.1 `tail`, with the counter-metric
  unchanged. NOT bge-m3, which this file once named on MIRACL scores and which
  came last or joint-last at four of five thresholds. failures.md #30.
- **Ranking**: three stages, `rrf w=0.20` over a 200-row pool then a
  `Qwen3-Reranker-0.6B` cross-encoder. **Reranking beats not reranking: 71.5%
  overall against 60.9%, +10.6% [+4.5%, +17.2%], 17 wins to 3, p=0.003**, and
  +9.1% on `specific`. 773ms rerank median, at ef_search 800 and batch 32.
  The MODEL choice is NOT supported by recall and must not be quoted as if it
  were - Qwen3 against bge is +2.7% [-2.1%, +7.6%] with 102 of 118 queries
  identical. gte-multilingual is excluded as incompatible with transformers
  5.x, which is not a quality judgement.
- **Two-stage baseline**, for comparison: `rrf w=0.20`, 60.9% overall.
- **Eval**: 118 labelled queries in three tiers - `core` (30), `specific` (44)
  and `tail` (44). `specific` and `tail` are mirror samplers differing only in
  review band, so the pair is a controlled contrast in target popularity. The
  tiers are NOT comparable to each other. `eval/queries_de.yaml` is a SECOND
  set: the same 118 targets asked in German, generated so app_ids are copied
  rather than retyped.
- **German recall** is 59.7% (core 21.7 / specific 75.0 / tail 70.5) at the
  shipped config, 55.1% at `rrf w=0.20`.
- **THE GERMAN GAP IS REAL AND IT IS 15 POINTS**, measured paired over 91
  matched pairs: EN 76.2% against DE 61.0%, -15.2% [-24.0%, -7.0%], 18 losses
  to 3, p=0.001. `specific` (-17.1%) and `tail` (-16.7%) both exclude zero;
  `core` (-9.2%) is not distinguishable. 70 of 91 tie, so it rests on 21.
- It is NOT an embedding-model problem (arctic's `specific` gain is entirely
  English) and NOT a reranker problem: reranking on German is +4.7%
  [-2.5%, +11.9%], p=0.189, against +10.6% on the mixed set. That claim is
  RETIRED, not deferred - the bigger set was built and the answer did not
  change. Do not quote the old 55.6 -> 77.8 figure. failures.md #37, #41.
- **Parsed search** (`--parse`, and every API request) is measured separately,
  because `run_eval`'s headline comes from the path WITHOUT the parser: overall
  60.0% against 55.8%, `specific` 84.1% against 72.7%. The gain is entirely
  `specific`.
- **Data**: `data/games.json`, 138,964 games. Use the JSON, not the CSV - the
  CSV is missing 13,109 games, has a 39-vs-40 column header offset, and has no
  `short_description` at all.
- **Environment**: Python 3.14.7 via uv; Ollama native on the host; Postgres
  16.15 + pgvector 0.8.6 in Docker.
- **README.md** leads with what does NOT work - weakest tier, the missing
  quality term, the German gap, six unretrievable tail targets, the full parser
  prompt. Keep it that way; every number in it is reproducible from `run_eval`.

**Open, in order:** a German document field in `embed_text` (the gap is now
attributed to the English corpus); a bounded queue with load shedding (30
concurrent searches take 209s each); trigram title matching for franchise names
(#21); and the six tail targets outside the 200-row pool, which need a
different stage 1.

Item 2 (hybrid sparse+dense) and item 3 (eval in CI) were dropped by decision,
not failure: item 2 targets proper-noun retrieval, which `title_lookup.py`
already handles, and its natural beneficiary is `core` - the tier that cannot
price anything.

- Commit per feature, not per session.
- When something breaks, three lines in `NOTES.md`: what broke, what I tried,
  what fixed it.

(update this at the end of every session)
