# Steam Vibe Search

Describe the *feeling* of a game you want, in English or German, and get ranked
results from 138,964 Steam games. A local LLM turns the sentence into structured
filters plus a semantic remainder; Postgres applies the filters, pgvector ranks
what survives.

> "co-op survival crafting with base building under 20 dollars on linux"
>
> parsed into `max_price_usd: 20.0`, `platforms: [linux]`, `multiplayer: true`
> and `required_tags: [Co-op, Survival, Crafting, Base-Building]`, leaving
> `semantic_query: "co-op survival crafting with base building"` for the vectors
> — 130,651 games narrowed to 768 by SQL, ranked by pgvector in 36ms, then
> reordered by a cross-encoder into Valheim, Terraria and Rising World.

Everything runs locally. No API keys, no hosted inference.

## Quickstart

You need **Docker**, **[uv](https://docs.astral.sh/uv/)**, **Node 22+**, and
**[Ollama](https://ollama.com/download) installed natively on the host** — not
in Compose, because embedding 130,651 games without GPU access takes hours
instead of 22 minutes. Python itself is uv's problem; it fetches 3.14.

**Linux or Windows, x86_64 or aarch64.** `pyproject.toml` binds torch to
PyTorch's CUDA index (see the trap at the end of this section), and that index
publishes no macOS wheels. torch and sentence-transformers live in a `rerank`
dependency group that uv installs by default, so on a Mac a plain `uv sync`
fails to resolve them. Not supported or tested there.

That group exists for the container, not for platforms: the backend image is
built with `--no-default-groups`, so it leaves out 3.0 GB of torch and CUDA
wheels it could never use — Compose pins it to `RANK_METHOD=rrf` because the GPU
is unreachable from inside a container. On the host nothing is different:
`uv run` installs the group and `run_eval` reranks out of the box.

### 1. Get the data

The corpus is the **[Steam Games Dataset](https://www.kaggle.com/datasets/fronkongames/steam-games-dataset)**
by Martin Bustos (fronkongames) on Kaggle — 138,964 games with descriptions,
tags, genres, prices, platforms and review counts, already scraped. Downloading
needs a free Kaggle account. Put the JSON here:

```bash
mkdir -p data
mv ~/Downloads/games.json data/games.json      # ~885MB, gitignored
```

**Take `games.json`, not `games.csv`.** The CSV is missing 13,109 games, has a
39-versus-40 column header offset that shifts every field from index 8 onward,
drops the tag vote counts that `embed_text` ranks by, and has no
`short_description` column at all. The loader reads only the JSON, and the path
is hardcoded to `data/games.json`.

### 2. Bring the stack up

```bash
ollama pull snowflake-arctic-embed2 && ollama pull qwen3.5:9b
cp .env.example .env
docker compose up -d
```

That brings up Postgres, applies the migrations and serves the API on `:8000`
and the UI on <http://127.0.0.1:5173>. The database is **empty** at this point:
Compose cannot fill it, because ingest needs both the host GPU and
`data/games.json`, which is outside the backend build context on purpose.

### 3. Load and embed

This runs on the host, and it is the slow step — about 25 minutes on an RTX
4080 SUPER, after a one-time download. The first `uv run` builds the Python
environment from `uv.lock`, fetching Python 3.14 if you don't have it and then
**2.1 GB of packages on Windows, 3.1 GB on Linux**, where CUDA comes as separate
`nvidia-*` wheels rather than inside torch. torch and CUDA are nearly all of it.
So the first command below takes as long as that download does, and every
`uv run` after it starts in seconds.

```bash
cd backend
uv run alembic downgrade 0006             # drop the vector index first
uv run python -m ingest.load_games        # ~3 min   -> 138,964 games
uv run python -m ingest.embed_all         # ~22 min  -> 130,651 vectors
uv run alembic upgrade head               # rebuild it, now the writing is done
```

**The two `alembic` lines are not optional.** Step 2 applied every migration,
and the last one builds the HNSW index — so a fresh database arrives with an
empty 1024-dimension index already in place, and `embed_all` refuses to start
while it exists:

```text
ix_games_embedding_hnsw exists. Writing 130k vectors with it in place
rebuilds the graph row by row.
```

That refusal is correct. Every row written needs a new entry in the graph, and
HNSW insertion is deliberately expensive, so building the index after the bulk
write rather than during it is the difference between seconds and many minutes.
It is the same rule migrations `0004` and `0006` follow internally.

**Stop at `0006`, never lower.** `downgrade 0006` runs only `0007`'s downgrade,
which drops the index and leaves the column alone. Going below it runs `0006`'s
downgrade, which re-dimensions `games.embedding` back to 768 with
`USING NULL::vector(768)` and discards every vector you just paid for.

Both ingest steps are idempotent and resumable; interrupt either and re-run it.
To watch the embed job from another terminal, `./backend/ingest/watch_embed.sh`
polls the database rather than the job's own output, which is buffered and
invisible when the run is backgrounded.

That is the whole setup. Search should now return results at
<http://127.0.0.1:5173>.

### Running from source instead

Stop the two app containers so they release the ports; the native workflow is
otherwise unchanged:

```bash
docker compose stop backend frontend
cd backend && uv run uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```

`npm install` is needed the first time: `node_modules` is gitignored, and the
containerised UI installs inside its own image, so a fresh clone has nothing to
run. `uv run` needs no equivalent — it syncs from `uv.lock` on first use.

Then open **`http://localhost:5173`**, not `127.0.0.1:5173` — Vite's dev server
binds IPv6 `::1` only and the numeric address refuses the connection. (The
containerised UI in step 2 is the opposite: nginx publishes the port normally,
so either spelling works. Both are in the API's CORS allowlist.)

Or skip the UI entirely:

```bash
cd backend
uv run python search.py "cozy farming game with fishing" --parse
uv run python -m eval.run_eval            # the numbers below
```

**Stage 3 needs an NVIDIA GPU on the host.** The cross-encoder is loaded
in-process by `app/rerank.py` — there is no server to start. The model is a
**2.4 GB** download the first time anything needs it, and the API starts that
download the moment it boots rather than inside your first search, so watch the
uvicorn terminal for `cross-encoder warm` (measured: 108s including the download,
8s once it is cached). `run_eval` and the CLI fetch it themselves on their first
run. If it crawls, set `HUGGING_FACE` in `.env` — unauthenticated Hub downloads
are rate limited. Without a GPU, set `RANK_METHOD=rrf` in `.env`: search
still works and drops to the two-stage numbers below. Left unset it will try,
fail to load, log a warning and fall back on every query, which is correct
behaviour but not worth watching. The containerised backend is already pinned to
`rrf` for exactly this reason.

One trap worth naming, because its only symptom looks like broken hardware: on
Windows, `uv add torch` installs a **CPU-only** wheel from PyPI without
complaining, and `torch.cuda.is_available()` simply returns `False`.
`pyproject.toml` therefore binds torch to PyTorch's own index explicitly.

## Architecture

```text
  "gemütliches Aufbauspiel für zwei, nichts Stressiges"
                     |
       query_parser  |  Ollama /api/chat, qwen3.5:9b, temperature 0,
                     |  output constrained to the ParsedQuery JSON schema,
                     v  grounded on all 452 real Steam tags
      ParsedQuery { max_price, platforms, required_tags, year, age,
                    multiplayer, ..., semantic_query }
                     |
        SQL filters  |  price / platform / tags / year / age / multiplayer
                     v  130,651 rows -> 622
            stage 1  |  HNSW index, ORDER BY embedding <=> query, LIMIT 200
                     |  ef_search 800, iterative_scan strict_order
                     v
            stage 2  |  reciprocal rank fusion over those 200
                     v    1/(60 + rank_cosine) + 0.20/(60 + rank_reviews)
            stage 3  |  cross-encoder scores all 200 (query, game) PAIRS,
                     |  Qwen3-Reranker-0.6B on the GPU, and its rank
                     v  replaces rank_cosine in the same sum above
                  top 10
                     |
                     |  ...then a SECOND request explains them: one line each
        /api/explain |  from qwen3.5:9b, every tag it cites checked against
                     v  games.tags, and 4.6% thrown away for failing that
```

Every stage above is timed on every request, logged as one line, and aggregated
into `GET /api/stats` as p50/p95 over the last 500 requests — see
[Where the time actually goes](#results).

Deliberately plain where it can be: no agent loop, no LangChain, one direct HTTP
call per model. The CLI, the API and the eval all call the same `search()`, so
there is one ranking implementation rather than three.

The parts that are *not* plain are the three-stage ranking and the parser
schema, and both are shaped by a measurement. pgvector only accelerates
`ORDER BY embedding <=> :v`, so a blended expression there silently drops to an
exact scan — the popularity term cannot live in stage 1 and has to rerank a
candidate pool instead. And `semantic_query` must be the **last** field in the
schema, because Ollama's structured output emits fields in declaration order and
that field is the query with every other constraint removed. Declared first,
both models returned the sentence unstripped.

Stage 3 is the one place a model is loaded **in-process** rather than reached
over HTTP, and that is not a preference. Ollama has no rerank endpoint at all
(`POST /api/rerank` is a 404, and every community workaround scores through the
*embedding* endpoint, which is the bi-encoder already in stage 1). Hugging Face
TEI does serve rerankers, but under Docker Desktop's WSL2 backend a container
gets working NVML and a CUDA driver API that answers `CUDA_ERROR_NO_DEVICE`, so
it starts on CPU with a warning and never finishes warming up. The honest cost of
running it in-process: **the containerised backend cannot rerank**, and
`docker compose` pins it back to stage 2 — the same limitation ingest already
has, for the same reason, written down rather than papered over.

| | |
| --- | --- |
| Backend | Python 3.14, FastAPI, SQLAlchemy 2.0, Pydantic v2, uv |
| Database | Postgres 16 + pgvector 0.8.6, HNSW (m=16, ef_construction=64), 1020MB index |
| Models | `snowflake-arctic-embed2` (1024-dim), `qwen3.5:9b` parsing, `Qwen3-Reranker-0.6B` reranking |
| Frontend | React 19, TypeScript, Vite |
| Data | 138,964 games, 130,651 embedded, 1,180,522 tags, 611,783 categories |

Three containers — `db`, `backend`, `frontend` — and three decisions inside
that worth naming:

- **The frontend publishes on host port 5173**, which is the port the API's CORS
  allowlist already names. Containerising the UI therefore needed no backend
  code change at all.
- **The backend reaches Ollama at `host.docker.internal`**, not `127.0.0.1`,
  which inside a container means the container. A containerised Ollama exists
  behind a `--profile ollama` flag for machines with no host install, but it is
  off by default because it would fight the host's for port 11434 and run on
  CPU.
- **The backend runs `alembic upgrade head` before uvicorn.** Idempotent,
  single replica, and the only way `docker compose up` reaches a working API on
  a clean volume without a README step everyone forgets.

Measured through the containers: 9–22ms warm query, ~90ms to embed the query
against ~48ms natively — that difference is the container-to-host network hop
to Ollama, and it is the price of keeping the GPU on the host.

## Results

118 labelled queries, `recall@10`, at the shipped configuration
(`snowflake-arctic-embed2`, RRF `w=0.20`, `Qwen3-Reranker-0.6B`, `ef_search=800`,
review threshold 10), against the same system with stage 3 switched off:

| tier | n | 2-stage | + cross-encoder | what it measures |
| --- | --- | --- | --- | --- |
| core | 30 | 16.1% | **27.8%** | short genre labels — "city builder", "deckbuilding roguelike" |
| specific | 44 | 79.5% | **88.6%** | a detailed description whose one right answer is popular |
| tail | 44 | 72.7% | **84.1%** | the same, but the right answer has 30–300 reviews |
| overall | 118 | 60.9% | **71.5%** | |
| English | 91 | 66.3% | 76.2% | |
| German | 27 | 42.6% | 55.6% | |

Reranking is worth **+10.6 points overall, 95% CI [+4.5, +17.2]** by a paired
bootstrap over queries, 17 wins to 3 (sign test p = 0.003) — see the last
section for why the interval is quoted and not just the number. It costs
latency: median search goes 54ms to 865ms, of which 773ms is the cross-encoder
scoring 200 pairs (p95 920ms). That is at a batch of 32 pairs; the original 128
cost 1,021ms and, beside the resident chat model, filled a 16GB card and pushed
p95 past 6 seconds — with the same top-10 sets.

These numbers replaced an earlier 68.8% on 2026-09-19. That figure was measured
at `ef_search=200`, where the result depended on which graph a non-deterministic
index build happened to produce — see *On the numbers above*.

Median reviews of everything returned: 169, with 72% under 1,000. That pair is a
counter-metric and matters more than the recall column — the reranker did not buy
its points by deleting the long tail.

**Where the time actually goes.** `GET /api/stats` keeps per-stage percentiles
over the last 500 requests. 50 searches through the API one at a time, nothing
excluded:

| stage | p50 | p95 | max |
| --- | --- | --- | --- |
| parse | 1,217ms | 1,493ms | 1,606ms |
| relax | 3ms | 11ms | 12ms |
| embed | 31ms | 37ms | 85ms |
| query | 46ms | 234ms | 400ms |
| rerank | 1,048ms | 1,816ms | 9,283ms |
| **total** | **2,397ms** | **3,206ms** | 10,901ms |

Measured at the original reranker batch of 128 and `ef_search` of 200; at today's
settings `run_eval` puts the rerank median at 773ms rather than 1,048ms.

Two things in that table were not what I expected. **The parser costs more than
the cross-encoder** — 1,217ms against 1,048ms at p50 — so the expensive stage is
the one nobody thinks of as a ranking stage, and the editable-chip path, which
skips it entirely, is worth more than it looks. And the relaxation ladder costs
**3ms** on real queries against the 11–24ms its design note claims, because that
figure was the worst case across scenarios and most queries pass their first
count.

`max` is where the honesty is: 9,283ms of reranking is one request paying the
cold model load, and it stays in the window. (Measured before the API loaded the
cross-encoder at startup; a request now pays that only if it arrives while the
warm-up is still running.) Excluding slow requests to make a
latency number look better is the failure this whole project is arguing against,
so nothing is dropped and `n` is reported beside every figure instead.

The three choices below were measured at the old `ef_search=200` and are
reported as measured then, not re-run.

**Choosing the embedding model.** Three models, same queries, same grid.
`snowflake-arctic-embed2` beat `qwen3-embedding:0.6b` by 6.7 points overall and
15.9 on `specific`; `bge-m3` came last or joint-last at four of five review
thresholds despite leading MIRACL by 13 points. The public leaderboards
predicted none of it.

**Choosing the ranking weight.** Cosine similarity has no notion of prominence.
For "city builder", a 78-review game literally named *City Builder* scores a
higher similarity (0.577) than *Cities: Skylines II* (0.502) and would win on
cosine alone. The rank-fusion term is what puts Skylines first without deleting
the small game from the results entirely, and the weight is `0.20` because that
is about the largest value that does not start costing long-tail recall.

**Choosing the reranker, and refusing to overclaim it.** Two models were
compared properly: `Qwen3-Reranker-0.6B` and `BAAI/bge-reranker-v2-m3`. Point
estimates said Qwen3 won by 2.7 points overall and 7.2 on `core`. A paired
bootstrap and a sign test said otherwise — **the two are not distinguishable**
(+2.7%, CI [-2.1%, +7.6%], 11 wins to 5, p = 0.105), with 102 of the 118 queries
scoring *identically*. So the choice was not made on recall.

It was made on a deterministic behaviour the eval cannot score: for "city
builder", bge drops *Cities: Skylines II* from rank 1 to **37**, behind that same
78-review game named *City Builder*, because a relevance-only cross-encoder
rewards literal topical match. Qwen3 — which can be given an instruction about
what relevance *means* — holds it at rank 1. bge is 4× faster for recall that
cannot be told apart, so on any latency budget it is the right call, and one
`.env` line switches it.

A third candidate, `gte-multilingual-reranker-base`, is excluded as incompatible
with transformers 5.x — which is not a quality judgement and is recorded as such.

## What doesn't work

**Short genre labels are still the weakest thing here** — 27.8% against 88.6%
for detailed descriptions, even after the cross-encoder took that tier up 11.7
points. Part of the gap is a measurement artifact: for "deckbuilding roguelike"
the system returns Roguebook and Beneath Oresa, which are correct, unlisted in
the ground truth, and score zero. Part of it is real. Both are true at once and
this tier cannot separate them, which is exactly why the reranker was chosen on a
named-game probe rather than on this number moving.

**Nothing in the ranking knows about review *quality*.** There is a popularity
term but no positivity term, and 4,592 searchable games sit under 50% positive.
That is the clearest missing feature. It was left out on purpose — it deserves
its own measurement rather than being bundled into the popularity work and
credited with its gains.

**Asking in German costs 15.2 points, and that is now measured properly.** Every
earlier EN/DE figure here was confounded: German was 37% `core` queries against
English's 22%, so part of the gap was tier mix, and the per-tier matrix that
fixed *that* still compared queries pointing at different games.

`eval/queries_de.yaml` holds the **same 118 targets and tiers** as the English
set, asked in German, so language is the only variable and the comparison is
paired query by query:

| | English | German | difference | sign test |
| --- | --- | --- | --- | --- |
| **overall, n=91** | 76.2% | 61.0% | **−15.2% [−24.0, −7.0]** | 3W 18L 70T, p = 0.001 |
| specific, n=35 | 91.4% | 74.3% | −17.1% [−31.4, −2.9] | 1W 7L 27T, p = 0.070 |
| tail, n=36 | 86.1% | 69.4% | −16.7% [−30.6, −2.8] | 1W 7L 28T, p = 0.070 |
| core, n=20 | 31.7% | 22.5% | −9.2% [−23.3, +2.5] | 1W 4L 15T, p = 0.375 |

So the overall gap is real. By tier, the `specific` and `tail` intervals both
exclude zero while their sign tests stop at p = 0.070, and the `core` gap is
**not** distinguishable from zero. At the old `ef_search=200` this table read as
"almost entirely detailed descriptions", with `tail` crossing zero; that
per-tier reading did not survive the re-measurement, and the overall one did.
70 of 91 queries tie, which is why the paired test matters: the whole result
rests on 21 queries and an unpaired comparison would have buried that.

The cause is that `embed_text` is English, so German queries are matched against
English descriptions. Swapping to the model sold on multilingual retrieval bought
20 points of English and zero German.

**And the cross-encoder does not fix it either — a claim this README previously
deferred and now retires.** The earlier reading was that reranking moved German
off a stuck 55.6%; it failed a paired test at n=9, and the stated next step was
to build a bigger German set. That set now exists, `specific` German went from 9
queries to 44, and the answer did not change: **+4.7% [−2.5%, +11.9%], 14 wins to
7, p = 0.189**, with `specific` at +9.1% [−2.3%, +20.5%]. Against +10.6% [+4.5%,
+17.2%] on the mixed set. Reranking's benefit is established in aggregate and
**not** established for German — at a sample size where that is now informative
rather than merely underpowered. The next thing to try is a German document
field, not a better reranker.

One honest limit on all of the above: these are German renderings of a fixed set
of requests, so they measure *the penalty for asking in German*, not how German
players actually phrase things.

**The long tail is searched, but only just.** 44 queries target games with
30–300 reviews and 84.1% come back. Reranking cannot rescue the rest: measuring
every labelled target's exact cosine rank shows **36 of 148 sit outside the
200-row candidate pool entirely**, so no reordering can reach them, and 75.7%
overall is the ceiling for any reranker here. Those are retrieval failures, and
fixing them needs a different stage 1, not a better stage 3. They are left in the eval rather than quietly dropped, because
removing the queries that score badly is how a benchmark starts flattering
itself.

**The explanation layer hallucinates a tag in 4.6% of cases**, which is why
every one is checked against the database before it is shown. A one-line "why
this matches" is generated per result, then verified: the tags it cites must be
tags the game actually has, and so must any tag it names in the prose. A failure
is discarded outright — no retry — and replaced by a deterministic line built
from the game's real tags, marked differently in the UI so a canned sentence is
never passed off as an explanation.

That 4.6% is a floor rather than a measure: it catches invented *tags*, and a
model that invents a plot detail out of the description passes every check. It
also started at 7.3%, and the difference was **my** bugs, not the model's —
auditing eight discards against their games' real tags showed the checker
punishing the model for denying a tag ("but does not include Fishing"), for a
sentence-initial verb that happens to be a tag ("Experience the daily life…"),
and for saying "Farming Sim" when `Farming` is separately a tag. All three
inflated the number, which is the direction that looks like diligence and
therefore never gets audited.

**New parser intents cannot go in the prompt.** It is full. Three separate edits
each silently destroyed a working filter, so intents like "popular" and
"excluding X" are regexes over the query text instead. The cost is that
unphrased variants get missed; the alternative cost a filter every single time.

**When filters starve the index, they get widened rather than returning an
empty page** — "No results under $10 — showing results under $20." A fixed
ladder gives up `required_tags` first (the vector still carries the intent),
then the review floor, the year, and finally doubles the price cap twice.

The interesting half is what it *refuses* to touch: age limits, excluded tags,
excluded games, the single/multiplayer axis and platform requirements are never
relaxed, and the loop returns a short page instead. A disappointing page beats a
confidently wrong one — showing horror to someone who said "nothing scary" is a
defect, not a compromise.

No model is in that loop. An agent would ask the LLM which constraint to drop;
this asks a table, in a fixed order, with a stopping condition. It is
reproducible, testable, free, and cannot invent a constraint that was never
there. It runs on capped `COUNT` queries rather than retried searches, so a
relaxed query still pays for exactly one embed and one rerank instead of three.
Design-time measurement put a capped count at 11–24ms depending on selectivity;
`/api/stats` since put the whole ladder at **3ms p50 over 50 sequential API
searches**, because the 11–24ms was the worst case across scenarios and most
queries stop at their first count. Either way it is against ~1,050ms of
cross-encoder per retry avoided. Under 30-way concurrency it rises to 30ms, which
is the database contending with itself, not the ladder doing more work.

**Franchise exclusion is coarse.** "excluding call of duty" is a prefix match, so
it drops all 24 entries — sequels and spinoffs included.

**It is a single-user system, and now there is a number for that.** Thirty
concurrent searches took **209 seconds each** — against 2.4s served one at a
time. One GPU is running Ollama's 6.6GB chat model and the in-process
cross-encoder, FastAPI's threadpool accepts every request, and nothing limits
how many pile onto the card: the per-request log shows single searches spending
70–148s in parse and 62–175s in reranking. Endpoints are `def` rather than
`async def` so no request blocks the event loop, which is the right call and
does nothing about GPU contention. A queue with a bounded depth, and shedding
load past it, is what this needs; there isn't one. This was found by the
observability work rather than assumed, which is roughly the point of it.

**`/api/stats` is a diagnostic, not telemetry.** It is a ring buffer in the
server process: it covers the last 500 *requests* rather than a period of time,
resets on restart, and would fragment across workers if the API were ever run
with more than one. It also sees API traffic only, so its p50 and the median
`run_eval` prints are different measurements and must not be quoted
interchangeably. The one guard that stops it lying outright: **p95 is withheld
until 21 requests**, because with nearest-rank percentiles anything below that
returns the maximum, and labelling the maximum "p95" is wrong rather than merely
rough. 21 is exact — at n=20 the index is still the last element. A first pass
used 20 and the guard silently did nothing at the boundary it existed to police.

**The eval cannot referee close calls, and now says so.** With ~100 of 118
queries scoring identically between two good configurations, it has far less
discriminating power than "118 labelled queries" suggests. It settled reranking
versus no reranking comfortably and could not separate two rerankers at all. A
paired bootstrap and a sign test are the precondition for any comparison table
here, and they now live in `eval/paired.py` with a self-test rather than in a
scratch script — `run_eval --dump` writes per-query scores and
`eval/compare_runs.py` pairs two of them, refusing dumps from different query
sets or rows that do not line up.

## On the numbers above

Every figure here is reproducible with `uv run python -m eval.run_eval`, and the
eval is built to resist flattering itself:

- **Three tiers, never compared to each other.** `core` understates quality
  because a correct-but-unlisted answer scores zero. `specific` and `tail` come
  from mirror SQL samplers that differ only in review band, which makes the pair
  a controlled contrast in target popularity rather than two arbitrary lists.
- **A counter-metric that needs no labels.** Median reviews returned, and the
  share under 1,000. Recall cannot see what a popularity weight *deletes*,
  because ground truth is a list of games somebody thought of, and people think
  of famous games. This metric uses neither labels nor query authorship, so
  neither bias reaches it.
- **A measured reproducibility floor.** Two embeds of the same model on the same
  corpus move `tail` by 2.3 points — one query out of 44 — when the corpus was
  embedded at two different batch settings. Differences below that are not
  results. That floor is why the ranking weight was *not* retuned when a finer
  sweep appeared to justify it.
- **The index build is not allowed to be a variable.** The HNSW index is built
  in parallel, and a parallel build is not deterministic: at the old
  `ef_search=200`, five builds of *identical* vectors scored 66.8–68.8%, and
  this README's first headline was one of those draws. The search now runs at
  `ef_search=800`, the smallest value at which three independent builds agree on
  every query of both sets, so the numbers no longer depend on which index a
  machine happens to build. It also recovered recall, which is reported below as
  a consequence, not as the reason.
- **Reproducible is not distinguishable**, and conflating the two produced three
  wrong headlines in a single session. The floor above measures re-running the
  *same* configuration; the uncertainty in a *difference between two* is much
  larger. Both rerankers reproduced their scores exactly across runs and were
  still statistically indistinguishable from each other. Comparisons here are
  now quoted with a paired-bootstrap interval, and the ones that cross zero are
  reported as crossing zero.

[`backend/eval/failures.md`](backend/eval/failures.md) documents 39 failure modes
with mechanisms, including several where a conclusion in this repo turned out to
be wrong and had to be withdrawn — the most recent being a reranker written off
as bad that turned out to be mis-invoked, and then a claimed win over the
alternative that did not survive a significance test. [`NOTES.md`](NOTES.md) is the running log of
what broke and what fixed it.
