# Steam Vibe Search

Describe the *feeling* of a game you want, in English or German, and get ranked
results from 138,964 Steam games. A local LLM turns the sentence into structured
filters plus a semantic remainder; Postgres applies the filters, pgvector ranks
what survives.

> "co-op survival crafting with base building under 20 dollars on linux"
>
> parsed into `max_price_usd: 20.0`, `platforms: [linux]`, `multiplayer: true`,
> leaving `semantic_query: "co-op survival crafting with base building"` for the
> vectors — 130,651 games narrowed to 1,867 by SQL, then Terraria, Volcanoids
> and Solace Crafting, in 68ms of query time.

<!-- TODO: record a GIF of the chip-editing interaction and drop it here. -->

Everything runs locally. No API keys, no hosted inference.

## Quickstart

Ollama runs **natively on the host** rather than in Compose, because embedding
130,651 games without GPU access takes hours instead of 22 minutes.

```bash
ollama pull snowflake-arctic-embed2 && ollama pull qwen3.5:9b
cp .env.example .env
docker compose up -d
```

That brings up Postgres, applies the migrations and serves the API on `:8000`
and the UI on <http://127.0.0.1:5173>. The database is **empty** at this point:
Compose cannot fill it, because ingest needs both the host GPU and
`data/games.json`, which is outside the backend build context on purpose.

So the last step runs on the host, and it is the slow one — about 25 minutes on
an RTX 4080 SUPER:

```bash
cd backend
uv run python -m ingest.load_games        # ~3 min   -> 138,964 games
uv run python -m ingest.embed_all         # ~22 min  -> 130,651 vectors
```

Both are idempotent and resumable; interrupt either and re-run it.

Prefer running the app from source instead? Stop the two app containers so they
release the ports, and the native workflow is unchanged:

```bash
docker compose stop backend frontend
cd backend && uv run uvicorn app.main:app --reload
cd frontend && npm run dev
```

Or skip the UI entirely:

```bash
cd backend
uv run python search.py "cozy farming game with fishing" --parse
uv run python -m eval.run_eval            # the numbers below
```

**Stage 3 needs an NVIDIA GPU on the host.** The cross-encoder is loaded
in-process by `app/rerank.py` — there is no server to start, and the model
downloads on first use. Without a GPU, set `RANK_METHOD=rrf` in `.env`: search
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
                     v  130,651 rows -> 1,867
            stage 1  |  HNSW index, ORDER BY embedding <=> query, LIMIT 200
                     |  ef_search 200, iterative_scan strict_order
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
(`snowflake-arctic-embed2`, RRF `w=0.20`, `Qwen3-Reranker-0.6B`, `ef_search=200`,
review threshold 10), against the same system with stage 3 switched off:

| tier | n | 2-stage | + cross-encoder | what it measures |
| --- | --- | --- | --- | --- |
| core | 30 | 17.8% | **27.2%** | short genre labels — "city builder", "deckbuilding roguelike" |
| specific | 44 | 79.5% | **88.6%** | a detailed description whose one right answer is popular |
| tail | 44 | 70.5% | **77.3%** | the same, but the right answer has 30–300 reviews |
| overall | 118 | 60.5% | **68.8%** | |
| English | 91 | 65.8% | 73.8% | |
| German | 27 | 42.6% | 51.9% | |

Reranking is worth **+8.3 points overall, 95% CI [+2.5, +14.5]** by a paired
bootstrap over queries — see the last section for why the interval is quoted and
not just the number. It costs latency: median search goes 84ms to 1,106ms, of
which 1,021ms is the cross-encoder scoring 200 pairs.

Median reviews of everything returned: 180, with 71% under 1,000. That pair is a
counter-metric and matters more than the recall column — the reranker did not buy
its points by deleting the long tail.

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

**Short genre labels are still the weakest thing here** — 27.2% against 88.6%
for detailed descriptions, even after the cross-encoder took that tier up 9.4
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

**German is about 22 points behind English.** Changing embedding model does not
fix it: swapping to the model sold on multilingual retrieval bought 20 points of
English and zero German. The cause is that `embed_text` is English, so German
queries are matched against English descriptions.

The cross-encoder is the first thing that has ever moved it — German went 42.6%
to 51.9%, and on detailed descriptions off a 55.6% that had been identical across
two embedding models. **That result does not survive a paired test** (5 wins to
2, p = 0.227) and is not claimed. It is a reason to build a larger German eval
set before building a German document field, which inverts the previous
conclusion: with 27 German queries, of which 9 are the tier that moved, this eval
cannot tell the difference between a fix and a coincidence.

**The long tail is searched, but only just.** 44 queries target games with
30–300 reviews and 77.3% come back. Reranking cannot rescue the rest: measuring
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

**Franchise exclusion is coarse.** "excluding call of duty" is a prefix match, so
it drops all 24 entries — sequels and spinoffs included.

**The eval cannot referee close calls, and now says so.** With ~100 of 118
queries scoring identically between two good configurations, it has far less
discriminating power than "118 labelled queries" suggests. It settled reranking
versus no reranking comfortably and could not separate two rerankers at all. A
paired bootstrap is now the precondition for any comparison table — but it lives
in a scratch script rather than in `eval/`, which is the next thing that should
be built there.

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
  corpus move `tail` by 2.3 points — one query out of 44. Differences below that
  are not results. That floor is why the ranking weight was *not* retuned when a
  finer sweep appeared to justify it.
- **Reproducible is not distinguishable**, and conflating the two produced three
  wrong headlines in a single session. The floor above measures re-running the
  *same* configuration; the uncertainty in a *difference between two* is much
  larger. Both rerankers reproduced their scores exactly across runs and were
  still statistically indistinguishable from each other. Comparisons here are
  now quoted with a paired-bootstrap interval, and the ones that cross zero are
  reported as crossing zero.

[`backend/eval/failures.md`](backend/eval/failures.md) documents 38 failure modes
with mechanisms, including several where a conclusion in this repo turned out to
be wrong and had to be withdrawn — the most recent being a reranker written off
as bad that turned out to be mis-invoked, and then a claimed win over the
alternative that did not survive a significance test. [`NOTES.md`](NOTES.md) is the running log of
what broke and what fixed it.
