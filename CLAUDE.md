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
- `cd backend && uv run python -m eval.run_eval` — recall, for RETRIEVAL and
  RANKING changes. Refuses to print a table if any query fell back from the
  reranker; a part-baseline run is not a weaker measurement, it is a different
  one wearing the model's label.
- `cd backend && uv run python -m eval.run_parse_eval` — for PARSER changes.
  Recall cannot referee those; see the Eval section.
- `cd backend && uv run python -m eval.run_explain_eval` — explanation
  hallucination rate. `--self-test` FIRST, and after any verifier change: it
  feeds four known-bad responses plus a truthful control, and a checker that has
  never gone red is not known to work.
- `cd backend && uv run python -m eval.run_eval --queries eval/queries_de.yaml`
  - the same 118 targets asked in German. A DIFFERENT SET is a different
  measurement, never a variant of the default one.
- `cd backend && uv run python -m eval.run_lang_eval` - EN vs DE on 91 matched
  pairs, with a sign test and a paired bootstrap. The only EN/DE number here
  that is not confounded by tier mix or target choice.
- `cd backend && uv run python -m eval.paired` - self-test the significance
  tests. Run it before believing any comparison; it is two-sided.
- `cd backend && uv run python -m eval.compare_runs a.json b.json` - paired
  comparison of two `run_eval --dump` files, for config-vs-config changes.
- `cd frontend && npm run dev`
- `cd frontend && npx tsc --noEmit && npm run lint` - typecheck and oxlint.
  Both are clean; oxlint is easy to forget because nothing in CI runs it.
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
  games. Embedding is cheap (~22 min on arctic), so scope is generous and the
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
- `required_tags` filters with `&&` (any-of), NOT `@>` (all-of). It was `@>` and
  that cost recall rather than buying precision: 55.4% against any-of's 60.0%
  over 118 queries, and of the 72 queries that got tags it helped 3 and hurt 11 -
  all three it helped carried exactly ONE tag. Six queries under-delivered and
  `running a bookshop and taking on cosmic horror` returned zero rows, because no
  game in 130,651 carries `Cozy` AND `Horror` AND `Investigation`; 8,544 carry
  one of them. The tag filter is a coarse recall gate and the vector does the
  discriminating - it is not there to rank. Co-op and versus intent is unaffected
  because that travels through `multiplayer` and `game_categories`, not tags.
  One GIN index serves both operators, so this needed no migration. `@>` is still
  right for `excluded_tags`, which is already `not_(overlap())` = shares none.
  See failures.md #33.
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
  That order only holds because `_REQUIRED_FIELDS` exists. An OPTIONAL property
  is a grammar branch the model may skip, and while every field but
  `semantic_query` was optional it emitted keys in whatever order it liked -
  `semantic_query` came out FIRST, which is exactly what declaring it last was
  meant to prevent. Adding a field to `ParsedQuery` without adding it there
  silently opts it out of both the ordering and the guarantee below.
- An optional field in a constrained-decoding schema is an invitation to OMIT it.
  `format` guarantees the output validates, never that it is complete, and a
  schema generated from Pydantic is almost entirely optional by accident: a field
  is required only when it has no default. The model returned `max_price_usd`
  ABSENT - not null - for "no wars on linux under 30$" while correctly stripping
  "under 30$" out of `semantic_query`, and absence is indistinguishable
  downstream from "no price asked for". It dropped `required_tags` on 64% of
  parses. `_REQUIRED_FIELDS` in `app/query_parser.py` is the fix and every scalar
  belongs in it. The tag ARRAYS deliberately do NOT: forcing those makes the
  model invent a tag for a vague query and costs 15.9 points of tail recall.
  Check what the grammar permits before blaming the prompt - #13, #22 and #32 all
  reached for prompt saturation, and this mechanism produces the same symptom.
  See failures.md #33.
- Making a field REQUIRED can make the model invent a value for it, and that is
  a different bug from the one you fixed. `platforms` required means the key must
  appear, and on a query naming no OS the model sometimes fills all three -
  measured 3/3 on "cheap relaxing puzzle games, nothing scary", 1 of 12 no-OS
  queries. Platforms are ANDed, so that silently demands Windows AND macOS AND
  Linux. `_drop_invented_platforms()` guards it, and only fires when the query
  names no OS, so a genuine "runs on windows, mac and linux" survives and the
  guard can only widen results. Leaving `platforms` optional is worse and was
  measured: the key then goes missing on "on linux under 30$". When checking a
  required-field change for invented values, check EVERY field you made
  required - the first pass here counted only the scalars and missed the one
  array. See failures.md #33.
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
  `/api/embed` with a batch. One-text-per-request is 30x slower. Never let an HTTP error
  from Ollama reach the caller without its body: `raise_for_status()` drops the
  one line that explains it, and a bare 400 cost an hour of bisecting a corpus
  whose longest row is 1,091 bytes. `_reason()` exists for that.
- Ollama packs several embed inputs into ONE server task and checks the packed
  token count against the physical batch, so an ordinary batch can be rejected
  while every text in it is tiny - it died on a 3,002-token task between rows of
  114 and 134 (NOTES.md 2026-09-06). `EMBED_NUM_BATCH=4096` raises that ceiling
  to the context; `_post_batch` halves a rejected batch and retries as the net
  above it. The WARNING per split is load-bearing: a silent split would hide a
  model that cannot handle the corpus at all.
- `verify_corpus_model()` runs before the first embed call in the CLI and in
  `run_eval`. `EMBED_MODEL` disagreeing with `games.embedding_model` is invisible
  at every other layer: arctic, bge-m3, qwen3-embedding and the column are all
  1024 dims, so the dimension check inside `embed_texts` passes and the query is
  simply compared against documents from a different space - no exception, no
  empty result, just quietly worse rankings. Switching models is one `.env` line
  plus `alembic downgrade 0006`, `embed_all --reload`, `alembic upgrade head`;
  stopping between any two of those leaves the halves disagreeing, and the eval
  would report the difference as a model result. A half-finished re-embed trips
  it too, which is correct. Deliberately NOT wired into `app/main.py` or
  `app/search.py` - that is a search-path edit and belongs in plan mode.
- `verify_corpus_complete()` is its sibling and catches what it cannot: the
  RIGHT model applied to only part of the table. During a `--reload` the model
  column agrees with `EMBED_MODEL` for the whole ~22 minutes while search runs
  against whatever fraction exists - fewer and worse results, no error. Only
  `run_eval` calls it, on purpose: a partial corpus is a normal thing to search
  from while ingest runs and the CLI should stay usable, but a recall number
  measured mid-reload is not merely imprecise, it looks exactly like a model
  result and would be written into a table as one.
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
- A ONE-WORD title is only a candidate when a reference CUE precedes it.
  `_word_ngrams` makes 2-to-5 word windows, and its docstring's claim that "one
  of them is the game's name, or its opening" is false for a one-word name —
  `Hades` has 279,741 reviews and was unreachable, as were Stray, Terraria,
  Factorio, Undertale and anyone typing just "like forza". Generating every
  single word instead is far WORSE than the bug and was measured: false positives
  over the 118 eval queries go from 4 to 24, because a bare word is not a claim
  about a title ("first person puzzle" matched `Persona 5 Royal` — `person`
  prefixes `Persona`), and each one appends six wrong tags to `semantic_query`.
  Gated on `_REFERENCE_CUE` it finds the same six titles and leaves false
  positives at 4. Cued singles are appended AFTER the longest-first windows, so
  "excluding call of duty" still resolves `phrase` to `call of duty` and not
  `call` — and `phrase` is what `wants_reference_excluded` looks for an excluder
  in front of, so that order is load-bearing twice. `MIN_NAME_LENGTH` is 5, not
  6, or `Hades`/`Stray`/`Forza` stay blocked. Re-measure the 4/118 before
  widening the cue list. See failures.md #35.
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
- Being IN the prompt is not the same as being reliable. `multiplayer` is in
  the prompt and the model fills it correctly on every short query, yet returned
  null on `call of duty like game, but not including itself, also popular,
  single player` — four competing clauses, with the ask last. Move "single
  player" earlier in that same sentence and it comes back. So a code rule is not
  only for intents the prompt cannot hold; it is also the net under the ones it
  already has. `wants_singleplayer()` FILLS, never overrides: the model was
  observed returning no value, never a wrong one, and overriding would break
  "single player or co-op". Any such regex needs a negation guard — a missing
  filter returns too much, a backwards one returns confidently wrong results.
  There is no `wants_multiplayer()` on purpose: "no multiplayer" contains
  "multiplayer". See failures.md #32.
- A constraint converted into a FILTER must stop steering the vector. Two
  separate leaks of this one rule: `apply_reference` borrowing contradictory tags
  (#32), and `wants_popular()` setting `min_reviews` while leaving the word
  `popular` in `semantic_query` — which then gets embedded and compared against
  descriptions, where it says nothing about what a game IS. That leaked on 8 of 9
  queries that asked for it. `_strip_popular()` reuses `_POPULAR` itself so the
  trigger and the removal cannot drift apart, and it is guarded twice: skipped
  when nothing would be left (a query of literally "popular" — `parse_query`'s
  empty check runs BEFORE `_apply_code_rules` and cannot catch it), and run
  before `apply_reference`, which would otherwise have the regex swept across its
  borrowed tag list. `wants_reference_excluded` STILL leaks this way and is worse
  — "excluding call of duty" embeds the excluded franchise's own name. Any new
  code rule needs to answer both halves: what filter does it set, and what does
  it remove from the text. See failures.md #34.
- German needs `\w*` on every adjective in a regex, because it inflects.
  `_POPULAR` carried bare `beliebt` next to `bekannt\w*`, so `beliebte
  Aufbauspiele` — the ordinary form, and the only one anyone writes — fired NO
  filter at all. Check the inflections, and check the near-miss: `beliebig`
  ("arbitrary") must not match, and does not, because it diverges before the `t`.
- `apply_reference` must never borrow a tag that contradicts the filters just
  extracted. The referenced game's tags describe the GAME, not the request, so
  "like resident evil but nothing scary" excluded `Horror` in SQL while
  appending it to the text being embedded — the WHERE clause deleting a category
  the ORDER BY was hunting. `_contradicts_filters()` checks `excluded_tags` and
  the multiplayer axis, which is why `_apply_code_rules` calls `apply_reference`
  LAST, after the code rules that set those. Exact match only: excluding
  `Horror` still borrows `Survival Horror`, because a substring rule would make
  an excluded `Action` drop `Action RPG` and `Action Roguelike`.
- Search ranking is two-stage: HNSW retrieves `RERANK_CANDIDATES` rows by pure
  cosine distance, then stage 2 reorders them with a popularity term. The blend
  can never go in the first ORDER BY - pgvector only accelerates
  `ORDER BY embedding <=> :v`, and any composite expression there silently drops
  to an exact scan. `RERANK_CANDIDATES` must not exceed `HNSW_EF_SEARCH`;
  config.py raises if it does, because stage 1 coming up short is silent.
- Search ranking is THREE stages when `RANK_METHOD=rerank`. HNSW retrieves
  `RERANK_CANDIDATES` by pure cosine, SQL computes the rrf blend, then a
  cross-encoder scores the whole pool in Python and its RANK replaces the COSINE
  rank inside that same rrf sum - same `k`, same `w`. Deliberately not a new
  formula: keeping it identical is what makes a rerank run comparable to the
  `rrf w=0.20` baseline instead of being a second variable, and it keeps the
  tail-cost counter-metric meaning what it meant. `POPULARITY_WEIGHT=0` collapses
  it to pure cross-encoder. The SQL blend is computed even in rerank mode because
  it costs nothing and is the fallback ordering when the model will not load.
- The reranker runs IN-PROCESS on the host GPU, which is the one model in this
  project that is not behind HTTP. Both serving routes are dead here and both
  fail silently: Ollama has no rerank endpoint at all (`POST /api/rerank` -> 404
  on 0.33.3, PR #7219 open since 2024, and every community workaround scores
  through the EMBEDDING endpoint, which is the bi-encoder we already have), and
  TEI cannot reach the GPU through Docker Desktop's WSL2 backend - the container
  gets working NVML and a CUDA driver API that answers CUDA_ERROR_NO_DEVICE, so
  it starts on CPU with a WARNING and never finishes warming up. Reproduced with
  a plain `docker run --gpus all`, so it is not the compose file. BUILD_PLAN
  sanctions this route in the same line as the Ollama one. The cost is real: the
  containerised backend CANNOT rerank, so docker-compose sets `RANK_METHOD=rrf`
  explicitly. See NOTES.md 2026-09-07.
- `uv add torch` on Windows installs a CPU-ONLY wheel from PyPI, silently, and
  the only symptom is `torch.cuda.is_available() == False` - which reads as a
  broken GPU rather than a wrong wheel. An unnamed `[[tool.uv.index]]` does not
  fix it either; resolution goes back to PyPI. It needs a NAMED index with
  `explicit = true` plus a `[tool.uv.sources]` binding, which is why pyproject
  carries both. `RERANK_DEVICE=cuda` is then checked at load, because
  sentence-transformers falls back to CPU without saying so and a CPU eval is not
  a slower measurement - TEI never finished warming up a 568M cross-encoder on
  CPU, and 118 queries x 200 candidates would run for hours.
- LOADING IS NOT SCORING, and the difference produced a wrong results table.
  `gte-multilingual-reranker-base` loads cleanly and raises a CUDA device-side
  assert on every `predict()`, so all 118 queries degraded to the SQL ordering
  and `run_eval` printed 60.5 / 17.8 / 79.5 / 70.5 - byte-identical to the
  control, reading as "no better" rather than "never ran". `verify_rerank_model()`
  now scores a probe pair and rejects CONSTANT scores too, because `_ranks` is a
  stable sort and constant scores reproduce the incoming cosine order exactly.
  `run_eval` refuses to print any table if a single query fell back: a run that
  is part baseline is not a weaker measurement, it is a different one wearing
  this model's label. See failures.md #36.
- A RELEVANCE-ONLY cross-encoder is worse at short genre labels, and recall
  cannot see it. `bge-reranker-v2-m3` drops Cities: Skylines II from rank 1 to 37
  for "city builder" - the query `w=0.20` exists to fix - because it rewards
  literal topical match and a 78-review game NAMED `City Builder` wins that,
  while `core` recall reports 17.8 -> 20.0, i.e. better, because core labels 2-3
  games out of hundreds that satisfy the query. An INSTRUCTION-FOLLOWING
  cross-encoder does not have this problem: Qwen3 puts it back at rank 1 and
  takes `core` to 27.2, the first time in this project a tier and a mechanism
  probe have agreed. bge scores -0.01 on FollowIR (at chance), Qwen3 5.41, and
  that benchmark predicted this where MTEB-R did not. Check a mechanism when a
  tier cannot price one - and prefer the model that can be TOLD what relevance
  means, because "how related are these two texts" is not the question a vibe
  search is asking.
- A reranker's input TEMPLATE is load-bearing and its absence is silent.
  `_pair_for()` in app/rerank.py wraps a pair the way each model expects, keyed
  on the model name like `_MODEL_PREFIXES` - Qwen3 needs its system prompt,
  `<Instruct>/<Query>/<Document>` tags and the assistant `<think>` preamble,
  because the yes/no logit it was trained to emit lands at exactly that position.
  Without it the model scored 8.1% overall and was nearly written off as bad.
  Before believing ANY reranker's recall number, compare SCORE SPREAD on a
  known-ordered triple: a mis-invoked model still ORDERS an easy triple correctly
  and merely loses conviction (0.121 against a correct 0.987), which is fine on
  three documents and useless across 200 similar games. One command, and it would
  have caught this before an eval ever ran. `RERANK_INSTRUCTION` is a tuned
  prompt and therefore a setting - see failures.md #37.
- Warm up a GPU model at the shape it will actually run. The first batch of a
  given size pays CUDA kernel selection - 9,668ms for the first 200-pair call
  against a 963ms steady state - and a 2-pair probe does not trigger the same
  kernels. That put a p95 of 6,234ms in a results table whose real p95 is
  1,376ms, which is not a latency tail, it is one query. `_load()` warms at
  `RERANK_CANDIDATES` for that reason.
- `RERANK_CANDIDATES` stays 200 and that is measured, not inherited. Every
  `specific` target a reranker can reach is inside rank 100 and 8 of 9 `tail`
  ones are; 200 -> 500 buys 16 more targets of which 14 are `core`, the tier that
  cannot price this. Reranking is O(pool) latency, so rows past the last
  recoverable target are latency bought for nothing. If VRAM or latency gets
  tight, lower `RERANK_BATCH_SIZE` first - the pool decides which targets are
  REACHABLE, the batch only decides how fast.
- A reranker's numbers mean nothing until you have checked it can actually
  discriminate, not merely rank. `Qwen3-Reranker-0.6B` scored 8.1% overall, which
  was an invocation bug and not its quality: the seq-cls conversion still needs
  the Qwen `<Instruct>/<Query>/<Document>` chat template, and a bare
  `(query, document)` pair yields near-zero logits. On a three-document probe it
  orders correctly with a spread of 0.121 against bge's 0.865 - right order, no
  conviction, which is fine on an obvious triple and useless over 200 similar
  games. Compare SPREAD on a known triple before believing a recall number.
  `gte-multilingual-reranker-base` is excluded separately as incompatible with
  transformers 5.x (`IndexError: index ... out of bounds for dimension 0 with
  size 30` from its remote code), not as worse.
- `RERANK_TRUST_REMOTE_CODE` executes Python from the model repo at load time.
  Off by default and opt-in per model, because a setting somebody has to type is
  a decision somebody made. `HUGGING_FACE` is a `SecretStr` so it cannot reach a
  log line or a traceback; app/rerank.py copies it into `HF_TOKEN`, which is what
  huggingface_hub actually reads. Unauthenticated Hub downloads are rate limited
  hard enough to stall a 1.2GB model at 9KB, which looks like a hang.
- `HNSW_EF_SEARCH=200`, not pgvector's default of 40. The default cost 3.3
  recall points at threshold 10 (15.0% against the exact scan's 18.3%) for 8ms.
- REPRODUCIBLE IS NOT DISTINGUISHABLE, and confusing the two produced three
  wrong headlines in one session. The floors below measure re-running the SAME
  config; the uncertainty in a DIFFERENCE between two configs is far larger. At
  n=118 with ~100 tied queries a 2.7-point gap has a 95% CI of [-2.1%, +7.6%],
  and 11 wins to 5 is p=0.105 ONE-SIDED, which is 0.210 two-sided - see the
  `eval/paired.py` note below before comparing that figure to anything. Run a
  paired bootstrap over queries and a sign test BEFORE writing a comparison
  table. The harness now exists: `eval/paired.py`, with `run_eval --dump` and
  `eval/compare_runs.py` for config-vs-config and `eval/run_lang_eval.py` for
  EN-vs-DE. See failures.md #37.
- Differences below ~2.5 points at n=44, or ~1 point at n=118, are NOT results.
  That is this eval's reproducibility floor, measured rather than guessed: two
  embeds of the same model on the same corpus move `tail` by 2.3 points, one
  query. Query time and the HNSW build are both byte-deterministic; the vectors
  are not. Check a difference against the floor before writing it down, and
  never change embedding batch settings mid-corpus - a corpus embedded two ways
  passes both `verify_corpus_model()` and `verify_corpus_complete()`, because
  one sees a single model name and the other sees no gaps. See failures.md #31.
- `run_eval --parse` has a SECOND floor on top of that one, and it is the
  parser, which is deterministic within a run and NOT across runs at
  `temperature=0` on the same model and text. First measured at one query
  (#32); then FOUR queries moved between two runs of identical code, which is
  3.4 points — so treat anything under ~3.5 points on `--parse` as noise, and
  budget three runs before believing a parser result. Two wrong diagnoses came
  out of not doing that: a per-query flip was attributed first to a platform
  guard and then to `semantic_query` over-stripping, and it was neither. When a
  `--parse` number moves, diff the per-query lines, prove the change can reach
  the query that moved, and re-run before writing a mechanism down.
  See failures.md #32 and #33.
- Ranking weight is chosen by the `tail` tier and the tail-cost counter-metric,
  NEVER by `core` or `specific` recall. Those two tiers' targets all sit above
  the 93rd percentile of the corpus by review count, so recall on them rises
  monotonically with the popularity weight however much tail it deletes - that
  is the ground truth's bias paid back to itself, not evidence (failures.md
  #26). The `tail` tier's targets sit at the 37th-75th percentile and behave
  correctly: flat to w=0.20, then falling. `run_eval` also prints median reviews
  returned and share under 1k, which need no labels at all.
  `rrf w=0.20` stands on arctic too, though for a different reason than it did
  on qwen3: on arctic `tail` is 72.7% at w=none and 70.5% from 0.125 up, so the
  weight costs one query, and the 0.10-versus-0.20 case is smaller than the
  reproducibility floor above. w=0.40 costs another and w=1.00 costs eleven.
  Nothing in the repo justifies a higher weight; if you want one, produce
  evidence from `tail` that clears the floor, not from `core`.
- Target obscurity is what lets an eval tier price a ranking weight. Query
  wording is not, and it was measured: rewriting queries "in a player's words"
  left content-word overlap with `embed_text` at 37% in both tiers and put MORE
  targets at cosine rank 1 than before. RRF pays a famous rank-1 target on both
  terms and pushes an obscure one down, so what matters is where the target
  sits in the corpus, not how the query reads. New tiers come from
  `sample_longtail.sql`; check its percentile column before writing a word. See
  failures.md #28.
- `docker compose up` runs db + backend + frontend. Three things in that file
  are load-bearing rather than stylistic. The frontend publishes on host 5173
  because that is the port `main.py`'s CORS allowlist already names, so
  containerising it needed no backend change - moving it breaks search from the
  UI with a CORS error and no server-side log. The backend reaches Ollama at
  `host.docker.internal` (with `extra_hosts: host-gateway`, which is what makes
  the same file work on native Linux), never `127.0.0.1`, which inside a
  container is the container. And `ollama` sits behind `--profile ollama` so it
  cannot take port 11434 from the host install that actually has the GPU.
- Ingest CANNOT run inside the backend container: the build context is
  `./backend`, so `data/games.json` is not in the image, and embedding wants the
  host GPU anyway. `docker compose up` therefore reaches a working API over an
  EMPTY database, which is the honest tradeoff and is documented in README's
  quickstart rather than papered over.
- The explanation layer's deliverable is the DISCARD RATE, not the sentence.
  `app/explain.py` writes one "why this matches" line per result and then checks
  every claim against `games.tags`; anything citing a tag the game lacks is
  thrown away and replaced by a deterministic line built from the real tags.
  Three properties make the check mean something and none is optional. The
  grounding data comes from the DATABASE, never the caller - `/api/explain`
  takes app_ids, not games, because a verifier fed client-supplied tags is
  checking the model against the client. A discard is FINAL: no retry, no second
  call, because retrying until it passes converts a measured failure rate into a
  hidden latency cost and the failure rate is the product. And `grounded=False`
  travels to the UI, which renders a fallback line differently and says so on
  hover - a canned sentence presented as an explanation is the exact failure the
  verification exists to prevent. See failures.md #38.
- A VERIFIER IS A MEASURING INSTRUMENT AND GETS MEASURED LIKE ONE. The first
  full run of the explanation check reported 7.3% hallucinated; an audit that
  printed eight discards next to each game's real tags found three of the eight
  were the checker's fault. All three bugs pushed the number UP, which is the
  safe-looking direction - a hallucination rate that reads too high looks like
  diligence, and nobody audits diligence. Print the evidence beside the ground
  truth and read some before publishing a rate.
- A prose scan over the tag vocabulary needs three guards, each found by audit
  rather than by reasoning. Overlapping tags resolve LONGEST-FIRST, because
  `Farming` and `Farming Sim` are both real and "it is a Farming Sim" otherwise
  matches both. A tag inside a NEGATED clause is a denial, not a claim - the
  prompt asks the model to hedge rather than invent, so "but does not include
  Fishing" is obedience, and the guard is scoped to the tag's own clause so
  "not a puzzle game but it is Souls-like" still flags `Souls-like`. And a
  SENTENCE-INITIAL single-word tag is grammar rather than a citation:
  `Experience` is both a real tag and an ordinary verb. Multi-word tags still
  count there. CLAUDE.md already carried the negation lesson for
  `wants_singleplayer()`; it applies to any regex over prose.
- `/api/explain` is a SECOND request, not part of `/api/search`. Search already
  costs ~1.1s of reranking, and an LLM call inline would hold the whole result
  list for a sentence nobody has scrolled to. Results render, explanations
  arrive after, and the frontend swallows their failure entirely - the list is
  correct and useful without them. Same instinct as the chip path staying
  model-free.
- `Explanation`'s field order is load-bearing for the same reason
  `semantic_query` must be last in `ParsedQuery`: `cited_tags` is declared
  BEFORE `why`, so the model commits to a tag list and then writes prose
  consistent with it rather than justifying finished prose. Every field is
  required by construction - none carries a default - and anything added needs
  that checked, because an optional property is a grammar branch the model may
  skip (#33).
- `run_explain_eval.py --self-test` must pass before any rate from it is
  believed, and after any change to the verifier. Four arms - an absent tag
  cited, one named only in prose, an app_id nobody asked about, and the model
  raising - each must be caught, plus a CONTROL with a real response, because a
  verifier that rejects everything would otherwise score perfectly. Knowing the
  checker fires correctly on lies is what made a suspicious rate worth
  investigating instead of explaining away.
- Query relaxation widens filters when they cannot fill a page, and its
  important half is the NEVER-RELAX list. A short page is a disappointment; a
  confidently wrong page is a defect. `max_required_age` (a safety constraint),
  `excluded_tags` (dropping it shows horror to someone who said none),
  `excluded_app_ids` (returns the game they excluded by name), `multiplayer`
  (returns the wrong KIND of game - #32 by another route) and `platforms` (a
  compatibility fact, not a preference) are never touched; the loop returns a
  short page instead. Verify that as BEHAVIOUR, not by reading the list.
- The relaxation loop runs on CAPPED COUNTS, never on retried searches. Since
  the cross-encoder landed, re-running `search()` per attempt costs ~1.1s each;
  a count over the same `_apply_filters()` is 11-24ms, and the LIMIT cap is what
  keeps it cheap (uncapped over 55,120 unfiltered rows is 611ms, because "how
  many" is a much harder question than "are there ten"). Walk the ladder on
  counts, then run exactly one real search. Reuse `_apply_filters` rather than
  restating the WHERE clauses: a count that disagreed with the real query about
  what a filter means would relax the wrong thing, silently.
- No model is in the relaxation loop, deliberately. An agent would ask the LLM
  which constraint to drop; this asks a table in a fixed order with a stopping
  condition, which is reproducible, testable, free, and cannot invent a
  constraint that was never there. The ladder ORDER is a judgement, not a
  measurement - there is no eval for "was that the right thing to give up" -
  and it says so in the code rather than pretending to be tuned.
- `run_eval` sets `relax_filters=False` and prints `relax: OFF`. Recall is
  measured against a FIXED filter set; a harness that widened filters whenever a
  query returned little would report the relaxation as retrieval quality. The
  no-parse path would never trigger it, but `--parse` would, and a number that
  only SOMETIMES includes a second mechanism is the worst kind.
- `SearchResponse.parsed` means what was ACTUALLY applied, so after relaxation it
  holds the widened filters and `relaxed` carries the diff. The chips must show
  the query that ran, not the one that was typed - and the UI renders the diff
  above the results, because a silently widened constraint is worse than a short
  page. Strings in `RelaxationStep.note` are ASCII: the CLI prints them too, and
  a Windows console renders an em dash as a replacement character.
- `/api/stats` is a ring buffer in `app/metrics.py`, and the numbers it returns
  are the easiest in this project to quote wrongly - so every one of them ships
  with its scope attached. It covers the last `STATS_WINDOW` REQUESTS, not a
  period of time; it is per-process and resets on restart; and it records at the
  ENDPOINT, so it sees API traffic only and its p50 is NOT the same measurement
  as the median `run_eval` prints. Recording at the endpoint rather than inside
  `search()` is the same instinct that keeps `app/games.py` out of
  `app/search.py`: the ranking path stays undiluted, and the CLI and the eval
  accumulate a window nobody reads.
- P95 IS WITHHELD BELOW 21 SAMPLES, AND 21 IS EXACT. With nearest-rank the index
  is `min(n-1, int(n*0.95))`, which only stops being the last element at n=21 -
  at n=20 it is `int(19.0) = 19 = n-1`, i.e. the maximum. Reporting the maximum
  under a p95 label is a WRONG LABEL, not an imprecise number, so it returns
  null with `n` beside it and the UI renders the reason rather than a blank. The
  first pass set the constant to 20 and the guard silently did nothing at exactly
  the boundary it existed to police; it was caught because the check script
  printed p95 next to max and they were the same value. Assert `p95 < max`, do
  not reason about it.
- A STAGE THAT DID NOT RUN IS OMITTED FROM THE RECORD, NEVER ZEROED. `parse_ms`
  is None on the chip path and `rerank_ms` is None wherever `RANK_METHOD` is not
  `rerank`, which is every container. A 0.0 would drag that stage's p50 toward
  nothing and read as a stage that costs nothing. Verified as behaviour: 27
  requests of which 4 were chip-path give `search.n=27` and `parse_ms.n=23`.
- NOTHING IS EXCLUDED from the window, including the cold start. One request
  paying the cross-encoder's first load put 9,283ms in `max` against a 1,048ms
  p50, and it stays there. Dropping slow requests to make a latency number look
  better is the failure this project argues against everywhere else; `max` is
  reported alongside p50 so the tail is visible rather than filtered.
- `metrics.note()` counts EVENTS, NOT REQUESTS - never divide one by
  `search.n`. Measured: with a bad `CHAT_MODEL` a single search reported
  `parse_call_failed: 2`, because `_warm_models()` parses at startup and that
  call really did fail. Counting it is right - a parser broken at boot should be
  visible before anyone searches - but the denominator is parser CALLS.
- The parse fallback is counted in `app/query_parser.py` rather than read off the
  response, because it is the one degraded path that leaves NO trace: it returns
  a bare `ParsedQuery`, which is byte-identical to a query that genuinely carried
  no constraints. It is deliberately NOT a new `ParsedQuery` field - the frontend
  posts that object straight back on a chip edit, so the flag would then describe
  a request in which no parse happened, and every field added there has to be
  checked against `_REQUIRED_FIELDS`, `_llm_schema()` and `describe()` (#33). `parse_call_failed` and `parse_bad_output` are split
  because one is Ollama being unreachable and the other is the model answering
  unusably - same product outcome, entirely different fix.
- THE PARSER COSTS MORE THAN THE CROSS-ENCODER, which nothing in this file
  predicted. Over 50 API searches: parse 1,217ms p50 against rerank's 1,048ms,
  with embed at 31ms and the SQL at 46ms. The expensive stage is the one nobody
  thinks of as a ranking stage, and it makes the editable-chip path - which skips
  parsing entirely - worth more than "14x faster" made it sound. Measure the
  whole pipeline before optimising the stage that looks expensive.
- Rerank latency is sensitive to VRAM CONTENTION and a run taken after other GPU
  work is not a measurement of the reranker. Two `run_eval` runs of identical
  code, minutes apart: 1,743ms median / 21,284ms p95, then 1,058ms / 1,573ms.
  The first followed four API restarts that each reloaded the cross-encoder
  alongside Ollama's resident 6.6GB chat model. Recall was byte-identical across
  both, which is what said the difference was environmental. Re-run before
  writing a latency number down, the same way `--parse` results need three runs.
- `SearchResponse.relax_ms` exists so the relaxation ladder's cost is checkable
  rather than asserted. Its design note claims 11-24ms per capped count; live
  traffic puts the whole ladder at 3ms p50, because that range was the worst case
  across scenarios and most queries stop at their first count. A claim that
  cannot be checked in production is an assertion.
- THE API IS SINGLE-USER UNDER LOAD, and item 6 is what produced the number.
  Thirty concurrent searches took 209 SECONDS EACH against 2.4s served one at a
  time - the per-request log shows single searches spending 70-148s in parse and
  62-175s in rerank. One GPU runs Ollama's resident 6.6GB chat model AND the
  in-process cross-encoder, FastAPI's threadpool accepts every request, and
  nothing bounds how many pile onto the card. `def` endpoints are still correct
  (no request blocks the event loop) and do nothing about GPU contention; the
  fix would be a bounded queue that sheds load, and there isn't one. Do not
  benchmark this API concurrently and read the result as latency.
- THE GERMAN GAP IS REAL AND IT IS 16 POINTS, measured paired for the first time.
  `eval/queries_de.yaml` holds the SAME 118 targets and tiers as queries.yaml,
  asked in German, so `run_lang_eval` compares 91 matched pairs where language is
  the ONLY variable - not tier mix, not target choice, both of which confounded
  every earlier EN/DE number. English 73.8% against German 57.7%, difference
  -16.1% [-25.8%, -6.8%], 20 losses to 4 wins, p=0.002. It is concentrated in
  `specific` (-25.7%, p=0.012) and is NOT distinguishable in `core` (-8.3%,
  p=0.375) or `tail` (-11.1%, p=0.289), so the overall figure is the result and
  the per-tier attribution holds only for `specific`. 67 of 91 queries tie, so
  the whole thing rests on 24.
- A SECOND QUERY SET GOES IN A SECOND FILE, never into queries.yaml. Recall is
  comparable across configs on a FIXED set and never across sets - going from 74
  to 118 queries once invalidated every number measured on the old one. Adding
  the German queries to queries.yaml would have killed every figure in README.md
  and this file. `run_eval --queries` selects the set and prints its name on the
  self-labelling line, because queries_de.yaml has the same size, tiers and
  targets and nothing else on screen would tell the two apart.
- Holding the TARGETS fixed is what makes a language comparison mean anything.
  The old EN/DE rows were never a language measurement - German was 37% `core`
  against English's 22%, so part of the gap was tier mix - and the per-tier
  matrix fixed that while leaving target choice, because the German queries
  pointed at different games. Same targets, same tiers, same expect: one
  variable. Same controlled-contrast logic as `specific` and `tail` being mirror
  samplers that differ only in review band.
- `eval/queries_de.yaml` is GENERATED, not hand-written, and app_ids are copied
  rather than retyped. queries.yaml's own header says a typo would look exactly
  like a recall failure; 148 expect entries transcribed by hand would produce
  one. `run_lang_eval` asserts row-for-row that `expect` and `tier` still agree
  between the files, because if they ever drift the comparison silently becomes
  two different questions.
- The German set measures the PENALTY FOR ASKING IN GERMAN, not how Germans
  search. These are German renderings of a fixed set of requests, so wording
  variance is controlled and naturalness is not. Genre loanwords stay English
  (Roguelike, Metroidvania, Souls-like, Deckbuilder) because that is how German
  gaming discourse writes them and translating them would measure a vocabulary
  nobody uses. Say which of the two questions a number answers before quoting it.
- `eval/paired.py` is the sign test and the paired bootstrap, and it is now the
  gate on every comparison claim. IT IS TWO-SIDED. CLAUDE.md's recorded
  Qwen3-vs-bge `p=0.105` is the ONE-SIDED tail of the same 11-5 split, which is
  0.2101 two-sided - the two numbers describe identical data under different
  conventions and must never be compared to each other. Two-sided is the default
  because "are these different" is not a directional hypothesis, and choosing the
  direction after seeing which arm won is what makes a one-sided test flattering.
  `uv run python -m eval.paired` self-tests both functions against hand-worked
  values, including that 11-5 split and a one-win-over-sixty-ties case that must
  come back p=1.0.
- `run_eval --dump` writes per-query scores and `eval/compare_runs.py` pairs two
  dumps. It REFUSES two dumps from different query sets, and refuses dumps whose
  rows do not line up - a paired test on misaligned rows silently throws away the
  ties that give it its power. The config travels inside the dump, so a file
  found a week later still says which model, set and rank method produced it.
- Commit per feature, not per session.
- When something breaks, three lines in `NOTES.md`: what broke, what I
  tried, what fixed it.

## Current state

Weekend 1 COMPLETE. Weekend 2 COMPLETE. Weekend 3 COMPLETE: 1024-dim re-embed,
the three-model comparison, the 118-query eval, `README.md`, and Compose
services for `backend` and `frontend`. Migrations 0001-0007.

`README.md` leads with what does NOT work, per BUILD_PLAN - weakest tier and
why, the missing quality term, the German gap, six unretrievable tail targets,
the full parser prompt. Keep it that way; every number in it is reproducible
from `run_eval` and several were corrected during writing because they had been
measured under the previous embedding model.

`.env.example` existed but had drifted: it still carried `EMBED_DIM`, which
`.env` itself documents as controlling nothing. It is now in sync and is the
only way a fresh clone can start, because `database_url` is the one setting in
`config.py` with no default and `.env` is gitignored.

API: `app/main.py` serves `POST /api/search`, `POST /api/explain`,
`GET /api/game/{app_id}`, `GET /api/health` and `GET /api/stats` over the same
`search()` the CLI uses — no second implementation. `SearchRequest` carries an optional `parsed`: when present its
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
`snowflake-arctic-embed2` at ~98 embeddings/sec at batch 128; Postgres 16.15 +
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

Embedded: all 130,651 rows carry a `snowflake-arctic-embed2` vector at 1024
dims, at 97.7/sec (22:16 over the corpus - see the table in ingest/embed_all.py). Migration `0003` built the HNSW index (`vector_cosine_ops`, m=16,
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

Parsed search (`--parse`, and every request through the API) is measured
separately from the pure-semantic numbers below, because `run_eval`'s headline
figures come from the path WITHOUT the parser and cannot see a parser change at
all. At `rrf w=0.20`, same-session matched runs: overall 60.0% against a
baseline of 55.8%, `specific` 84.1% against 72.7%, `core` 16.1% against 12.8%,
`tail` 65.9% against 68.2%. The gain is entirely `specific`; `tail` is down 2.3,
which is the floor. Parse costs ~1.07s against ~0.56s before, so the API's
first-search path is ~1.85s - the chip path still does not re-parse and stays at
0.095s.

The eval CANNOT referee a parser change on its own: 7 of its 118 queries carry
anything constraint-like and none names a price, platform, year or age, so every
filter the parser extracts can only shrink the result set. Strip the tag arrays
out of the schema and `--parse` scores exactly the no-parse baseline. Judge
constraint extraction with `compare_parsers.py`, not with recall.

Failure modes: `backend/eval/failures.md`, 35 documented with mechanisms. That
file is the raw material for `eval/queries.yaml` and for the README's "what
does not work" section.

Eval: 118 labelled queries in three tiers - `core` (30, short genre labels
answered by famous games), `specific` (44, detailed descriptions with one right
answer above the 93rd percentile) and `tail` (44, the same but the answer has
30-300 reviews). `specific` and `tail` come from mirror samplers
(`sample_specific.sql`, `sample_longtail.sql`) that differ only in review band,
so the pair is a controlled contrast in target popularity - hand-picking either
one breaks that. The tiers are NOT comparable to each other: core recall
understates quality because a correct-but-unlisted answer scores zero, and tail
recall includes targets pure cosine cannot retrieve at all, left in
deliberately. Compare a tier against itself across configs, and read `tail` plus
the tail-cost counter-metric before believing any ranking number.

`eval/queries_de.yaml` is a SECOND set, not an extension of the first: the same
118 targets and tiers asked in German, generated from queries.yaml so the
app_ids are copied rather than retyped. It exists because queries.yaml is frozen
(adding to it would invalidate every published number) and because a language
comparison needs the targets held fixed to mean anything. `run_eval --queries`
selects it, `run_lang_eval` runs the 91 matched pairs against English with a
sign test and a paired bootstrap. German recall on it is 56.4% overall
(core 21.7 / specific 68.2 / tail 68.2) at the shipped rerank config, and 52.5%
at `rrf w=0.20`.

`run_eval` CANNOT referee a parser change, and three in a row had to be justified
without it (#33, #34, #35). Not one of its 118 queries names a price, a platform,
a year, an age or a game, so every filter the parser extracts can only shrink the
candidate set - recall punishes extraction and can never reward it. Proof it is
blind rather than merely unkind: strip the tag arrays out of the schema entirely
and `--parse` scores exactly the no-parse baseline. `eval/run_parse_eval.py`
against `eval/parse_cases.yaml` is the instrument for that, scoring what recall
cannot see - constraints MISSED, and constraints INVENTED (any scalar field a
case does not name must come back null, so invention is caught with no extra
labels). It also checks that a constraint which became a filter left
`semantic_query`, which is the #34 rule. Use recall for retrieval changes and
this for parser changes; a parser change that moves recall is usually the #33
noise. It defaults to 3 reps and reports a `flaky` column, because a single pass
would be as unreliable as the thing it measures. Cases marked `known_gap` are
documented limitations kept in the file and out of the score - the same refusal
to flatter as leaving unretrievable targets in `queries.yaml`.

Recall is not comparable across query SETS either, only across configs on a
fixed set. Expanding the eval from 74 to 118 invalidated every number measured
on the old set, including the qwen3 model baseline. Budget a re-run of anything
you want to keep comparing before adding queries.

The aggregate EN/DE rows are not a language measurement: German is 37% `core`
queries against English's 22%, so a chunk of any gap is tier mix. `run_eval`
prints a tier-by-language matrix - read that. On arctic at w=0.20 the aggregate
gap was 24.3 points and the per-tier gaps were 4.2 / 30.2 / 12.5. See
failures.md #29.

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

Weekend 3 in progress. The embedding model is **`snowflake-arctic-embed2`**,
picked by measurement over `qwen3-embedding:0.6b` and `bge-m3` on 118 labelled
queries. It is NOT bge-m3, which this file once named as the target on the
strength of its MIRACL score and which came last or joint-last at four of five
review thresholds. Query/document
prefixes are keyed on the model name in `app/embedding.py`, which also lands the
nomic prefixes failures.md #19 wanted.

Arctic beats qwen3 by 7.5 recall points overall at `rrf w=0.20`, 15.9 on
`specific` and 9.1 on `tail`, with the tail-cost counter-metric unchanged (74%
against 75% under 1k) - so it is not buying recall by deleting the long tail. It
wins at `w=none` too, so it is retrieval quality rather than an interaction with
the ranking term. qwen3 wins `core` alone by 7.2, and that is the tier this file
already documents as understating quality.

**#25's threshold crossover was an artifact and its conclusion is withdrawn.**
Raising `REVIEW_THRESHOLD` deletes rows from the corpus; it does not ask for an
obscure game. The `tail` tier asks directly and arctic wins it. Do not use a
threshold sweep as a proxy for long-tail retrieval again - that is what `tail` is
for. See failures.md #30.

SHIPPED. All 130,651 rows carry an arctic vector, `.env` and the `config.py`
default both name it, and the index is rebuilt. At `rrf w=0.20`: core 17.8%,
specific 79.5%, tail 70.5%, overall 60.5%, median 147 reviews returned, 74% under
1k, 84ms. (Measure the incumbent FIRST next time - measuring it second cost an
extra 30-minute round trip back to the winner.)

German is NOT an EMBEDDING model problem. Arctic's `specific` gain is entirely
English - 65.7 -> 85.7 while German sits at 55.6% for both models - so swapping
to the multilingual embedding model bought 20 English points and zero German
ones. It is NOT a reranker problem either, and that claim is now RETIRED rather
than deferred. The hypothesis was that Qwen3-Reranker moves `specific` DE off
that stuck 55.6% to 77.8%; this file said it did not survive a paired test at
n=9 and was "a reason to BUILD A BIGGER GERMAN SET and nothing more". The bigger
set exists now - `eval/queries_de.yaml`, the same 118 targets asked in German,
so `specific` DE went 9 -> 44 - and the answer did not change. Reranking on
German is +3.8% overall [-3.0%, +11.0%], 13 wins to 7, p=0.263, and `specific`
DE is +9.1% [-2.3%, +20.5%], p=0.289. Against +8.3% [+2.5%, +14.5%] on the mixed
118. So the reranker's benefit is established in AGGREGATE and not established
for German, at a sample size where that is now informative rather than merely
underpowered. Do not quote the 55.6 -> 77.8 number. failures.md #37 and #41.

Ranking: THREE stages as of Weekend 4 - `rrf w=0.20` over a 200-candidate pool,
then a `Qwen3-Reranker-0.6B` cross-encoder whose rank replaces the cosine one
inside that same rrf sum. **The claim this supports is that reranking beats not
reranking: +8.3% overall [+2.5%, +14.5%] and +9.1% on `specific` [+2.3%, +18.2%],
paired bootstrap over queries.** 68.8% overall against the two-stage 60.5%,
1,021ms median. DEFAULT in config.py, overridden back to `rrf` in
docker-compose because the container has neither torch nor the GPU.

The MODEL choice is NOT supported by recall and must not be quoted as if it were.
Qwen3 against `bge-reranker-v2-m3` is +2.7% [-2.1%, +7.6%], 11 wins to 5 with 102
of 118 queries identical, sign test p=0.105. Nothing separates them - not `core`,
not `specific`, not German. Qwen3 is the default on the DETERMINISTIC mechanism
probe instead: bge drops Cities: Skylines II from rank 1 to 37 for "city
builder", Qwen3 holds it at 1. On a latency budget bge is the same recall at a
quarter of the cost. gte-multilingual is excluded as incompatible with
transformers 5.x, which is NOT a quality judgement. See failures.md #36 and #37.

Two-stage baseline, for comparison: `rrf w=0.20` over a 200-candidate pool. recall@10 is 25.0 / 26.7 /
30.0 / 41.1 / 42.2% across the five thresholds, from 18.3 / 22.8 / 26.7 / 38.3 /
38.9. Query time 44-85ms. `run_eval`'s latency figure includes the embedding
call, so it is not a query measurement - Ollama swung 94-834ms after a host
restart and made ranking look 20x slower than it is.

WEEKEND 4 IS DONE AND THE PROJECT IS WOUND UP. Item 1 reranking, item 4 grounded
explanations, item 5 query relaxation and item 6 observability are shipped; item
3 (eval in CI) and item 2 (hybrid sparse+dense) were both dropped by decision,
not by failure. Item 2 was argued against on evidence rather than effort: it
targets proper-noun retrieval, which `app/title_lookup.py` already handles by
borrowing a named game's tags, and its natural beneficiary is `core` - the tier
this file documents as unable to price anything. Nothing in the repo predicts it
would move `tail`.

WEEKEND 5 (discount likelihood) IS SKIPPED, deliberately and on a checkable
reason. BUILD_PLAN makes it conditional on a price collector running from day one
of Weekend 1 - "by the time you reach this weekend you'll have several weeks of
your own series" - and `price_history` exists nowhere in this repo except inside
BUILD_PLAN's own text. Without that series there is no temporal split, no
calibration curve, and none of the cross-source discrepancy story that was the
point; the only price data here is a single Kaggle snapshot taken DURING a Steam
sale, which is why `list_price_usd` is a generated column. It is also a different
project - tabular time-series ML sharing a database with a retrieval system - and
BUILD_PLAN itself calls it the highest-risk item in the file, because a wrong
probability looks authoritative in a way a mediocre search result does not.

The last thing built was the bigger German set that #37 asked for, and it
returned a negative: see the German paragraph above. What remains open and is
worth doing next, in order: a German document field in `embed_text` (the German
gap is 16 points and is now attributed to the English corpus rather than to the
reranker); a bounded queue with load shedding (30 concurrent searches take 209s
each); trigram title matching for franchise names (#21); and the six tail targets
that sit outside the 200-row pool, which need a different stage 1.

Still worth carrying: trigram title matching for franchise names with
™/edition suffixes (failures.md #21).
`SearchResponse` carries `parsed`; posting it back with a filter removed is the
chip interaction, and it costs no LLM call.

(update this at the end of every session)
