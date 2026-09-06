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
            stage 2  |  rerank those 200 by reciprocal rank fusion
                     v    1/(60 + rank_cosine) + 0.20/(60 + rank_reviews)
                  top 10
```

Deliberately plain where it can be: no agent loop, no LangChain, one direct HTTP
call per model. The CLI, the API and the eval all call the same `search()`, so
there is one ranking implementation rather than three.

The two parts that are *not* plain are the two-stage ranking and the parser
schema, and both are shaped by a measurement. pgvector only accelerates
`ORDER BY embedding <=> :v`, so a blended expression there silently drops to an
exact scan — the popularity term cannot live in stage 1 and has to rerank a
candidate pool instead. And `semantic_query` must be the **last** field in the
schema, because Ollama's structured output emits fields in declaration order and
that field is the query with every other constraint removed. Declared first,
both models returned the sentence unstripped.

| | |
| --- | --- |
| Backend | Python 3.14, FastAPI, SQLAlchemy 2.0, Pydantic v2, uv |
| Database | Postgres 16 + pgvector 0.8.6, HNSW (m=16, ef_construction=64), 1020MB index |
| Models | `snowflake-arctic-embed2` (1024-dim), `qwen3.5:9b` for parsing |
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
(`snowflake-arctic-embed2`, RRF `w=0.20`, `ef_search=200`, review threshold 10):

| tier | n | recall@10 | what it measures |
| --- | --- | --- | --- |
| core | 30 | 17.8% | short genre labels — "city builder", "deckbuilding roguelike" |
| specific | 44 | **79.5%** | a detailed description whose one right answer is popular |
| tail | 44 | **70.5%** | the same, but the right answer has 30–300 reviews |
| overall | 118 | 60.5% | |
| English | 91 | 65.8% | |
| German | 27 | 42.6% | |

Median search 84ms. Median reviews of everything returned: 147, with 74% under
1,000 reviews. That second pair is a counter-metric and it matters more than the
recall column — see the last section.

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

## What doesn't work

**Short genre labels are the weakest thing here** — 17.8% against 79.5% for
detailed descriptions. Part of that is a measurement artifact: for "deckbuilding
roguelike" the system returns Roguebook and Beneath Oresa, which are correct,
unlisted in the ground truth, and score zero. Part of it is real, though. The
same query surfaces a 44-review, 50%-positive game called *Megacity Builder* for
"city builder". Both are true at once and this tier cannot separate them.

**Nothing in the ranking knows about review *quality*.** There is a popularity
term but no positivity term, and 4,592 searchable games sit under 50% positive.
That is the clearest missing feature. It was left out on purpose — it deserves
its own measurement rather than being bundled into the popularity work and
credited with its gains.

**German is about 23 points behind English**, and on detailed descriptions the
gap is 30. Changing embedding model does not fix it: swapping to the model sold
on multilingual retrieval bought 20 points of English and zero German. The cause
is that `embed_text` is English, so German queries are matched against English
descriptions. A German document field is the fix and it is not built.

**The long tail is searched, but only just.** 44 queries target games with
30–300 reviews and 70.5% come back. Six of those targets are not in the top 200
under pure cosine *at all* — genuine retrieval failures no amount of reranking
can recover. They are left in the eval rather than quietly dropped, because
removing the queries that score badly is how a benchmark starts flattering
itself.

**New parser intents cannot go in the prompt.** It is full. Three separate edits
each silently destroyed a working filter, so intents like "popular" and
"excluding X" are regexes over the query text instead. The cost is that
unphrased variants get missed; the alternative cost a filter every single time.

**Franchise exclusion is coarse.** "excluding call of duty" is a prefix match, so
it drops all 24 entries — sequels and spinoffs included.

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

[`backend/eval/failures.md`](backend/eval/failures.md) documents 31 failure modes
with mechanisms, including several where a conclusion in this repo turned out to
be wrong and had to be withdrawn. [`NOTES.md`](NOTES.md) is the running log of
what broke and what fixed it.
