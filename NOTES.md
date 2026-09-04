# Notes

What broke, what I tried, what fixed it. Newest first.

---

## 2026-09-04 — The 1024-dim HNSW index is 2x the size and quietly costs recall

**What broke.** Two predictions about migration `0007` were wrong at once. The
plan estimated ~680MB for the rebuilt index (510MB scaled by 1024/768); it came
out at **1020MB**. And re-running the eval with the index in place scored
*lower* than the exact scan it replaced - 15.0% against 18.3% at threshold 10.

**What I tried.** For the size, divided it by the page size instead of guessing:

    pages 130,608   elements 130,651   pages_per_element 1.000

Exactly one 8KB page per element. A 1024-dim float32 vector is 4,096 bytes;
with the m=16 neighbour list and tuple overhead an element lands near 4.4KB, so
two cannot share an 8KB page and each one wastes ~45%. At 768 dims an element is
~3.4KB and two fit, which is why 510MB looked like the baseline to scale from.
The jump is a **packing cliff at 4KB, not a 33% dimension increase** - the cost
is a step function of dimension, and 1024 sits just past the step.

For the recall, the index is approximate and `hnsw.ef_search` was never set, so
it ran at pgvector's default of 40. `search.py` sets `iterative_scan` and
nothing else.

**What fixed it.** `hnsw.ef_search = 200` restores exact-scan recall exactly, at
every threshold:

| threshold | exact scan | ef_search 40 | ef_search 200 |
| --- | --- | --- | --- |
| 10 | 18.3% | 15.0% | 18.3% |
| 100 | 22.8% | 19.4% | 22.8% |
| 1,000 | 26.7% | 23.3% | 26.7% |
| 10,000 | 38.3% | 38.3% | 38.3% |
| 50,000 | 38.9% | 38.9% | 38.9% |

45ms -> 53ms for those 3.3 points, against 225ms for the exact scan that scores
the same. Not applied: it changes which results come back, so it is a ranking
change and goes through plan mode. It belongs beside the existing
`SET LOCAL hnsw.iterative_scan` in `app/search.py`, not in the database - it was
measured with `ALTER DATABASE ... SET`, which has since been reset, because a
GUC pinned on the database is exactly the invisible drift the Alembic rule
exists to prevent.

Note the loss is zero at 10,000 and above. A selective filter with
`strict_order` already forces the scan to keep going until it has enough rows,
so the approximation only bites when the filter is loose. Low threshold is both
where recall is hardest *and* where the index hurts most.

Worth carrying: `halfvec` at 1024 dims is 2,048 bytes, which fits two or three
to a page and would roughly halve the index. pgvector indexes halfvec up to
4,000 dimensions. Untested here.

---

## 2026-09-04 — No leaderboard picked the winner, and the winner depends on the threshold

**What broke.** The first model comparison was run piecemeal — one embed job
was interrupted and resumed, and bge-m3's pre-eval check used
`min(embedding_model)`, which returns `bge-m3` whether or not arctic vectors
are still sitting in the same column. So the numbers might have been measured
over a mixed corpus, and there was no way to tell after the fact.

**What I tried.** Re-ran all three end to end: `--reload`, then the same
verification query before every eval — `count(DISTINCT embedding_model)` (not
`min`), distinct `vector_dims`, pending count, and a degenerate-vector check via
`(embedding <#> embedding) = 0`, which is `-||v||^2` and exactly zero only for
the zero vector. pgvector 0.8.6 has no `l2_norm(vector)`, only halfvec and
sparsevec overloads, and casting would round small components to zero and report
false positives.

**What fixed it.** Nothing needed fixing: all thirty numbers reproduced exactly.
The `embedding IS NULL` work queue means a resumed job writes the same vectors
as an uninterrupted one — that is what it is for, now demonstrated instead of
assumed. The verification gap was real; the results it threatened were not.

recall@10, exact scan, no HNSW, per-query average:

| threshold | corpus | arctic-embed2 | bge-m3 | qwen3:0.6b |
| --- | --- | --- | --- | --- |
| 10 | 55,120 | 8.3% | 10.0% | **18.3%** |
| 100 | 22,700 | 17.8% | 10.0% | **22.8%** |
| 1,000 | 7,212 | 26.1% | 22.8% | **26.7%** |
| 10,000 | 1,702 | **57.2%** | 49.4% | 38.3% |
| 50,000 | 470 | **47.2%** | 44.4% | 38.9% |

**The ranking inverts between 1,000 and 10,000.** Below the crossover qwen3 wins
by 10 points; above it arctic wins by 19. So "which embedding model is best" has
no answer here without naming the review threshold — the two models are good at
different things. Read together with the entry below: at low thresholds the
corpus is mostly shovelware whose *names* restate the query, and arctic is more
easily fooled by that; at high thresholds the shovelware is gone and arctic's
stronger raw retrieval is what remains.

Shipped qwen3, because `REVIEW_THRESHOLD=10` is the configuration that ships.
**Re-measure after ranking gets a popularity term** — that moves the effective
regime toward the clean-corpus end, where arctic wins. A ~26 min re-embed
settles it; do not assume the choice survives.

Also: no leaderboard predicted this. bge-m3 leads MIRACL by 13 points (69.2 vs
55.8) and finished last or joint-last at four of five thresholds. arctic leads
MTEB Retrieval (55.6 vs 48.8) and loses at the threshold in use. CLAUDE.md named
bge-m3 as the Weekend 3 target on that evidence, and it is the worst of the
three here. The plan flagged this risk — MIRACL is monolingual DE→DE while this
is DE query against EN documents — and the measurement confirmed it.

DE never trails EN for qwen3 at any threshold, which no other model managed. At
n=10 German queries that is suggestive, not a result.

---

## 2026-09-04 — The embedding model was not the problem; ranking was

**What broke.** Baseline recall@10 was 6.1% overall (9.2% EN, 0.0% DE) on
`nomic-embed-text`. DE at zero reads as a German problem, but the German
queries are near-parallel to the English ones, so DE's ceiling *is* EN's 9.2%.
Both columns being bad pointed at the model — an English-only 2024 model
against a bilingual corpus.

**What I tried.** Re-dimensioned to 1024 (migration `0006`) and re-embedded all
130,651 games with `snowflake-arctic-embed2`, which leads MTEB Retrieval (55.6)
and CLEF (54.1) among indexable candidates. Result: 8.3% overall — DE unstuck
(0.0% -> 10.0%) but **EN went down**, 9.2% -> 7.5%. A better model moved
almost nothing.

**What fixed it.** Reading the actual results instead of the score. Top 10 for
`city builder`:

    City Builder / Megacity Builder / 20 Minute Metropolis - The Action City
    Builder / Constructor Plus / City Block Builder / Square City Builder /
    Epic City Builder 4 / Cities: Skylines II / ...

Every game whose *name* contains the query outranks Cities: Skylines II and its
73,524 reviews. Cosine similarity has no notion of prominence, and in a corpus
that is mostly shovelware a nameless asset-flip called literally "City Builder"
wins the lexical match every time. The retrieval was never broken; the ranking
has no quality term.

Sweeping `REVIEW_THRESHOLD` (arctic-embed2, recall@10, exact scan, no HNSW):

| threshold | corpus | EN | DE | overall |
| --- | --- | --- | --- | --- |
| 10 | 55,120 | 7.5% | 10.0% | 8.3% |
| 100 | 22,700 | 19.2% | 15.0% | 17.8% |
| 1,000 | 7,212 | 26.7% | 25.0% | 26.1% |
| 10,000 | 1,702 | 63.3% | 45.0% | **57.2%** |
| 50,000 | 470 | 45.8% | 50.0% | 47.2% |

7x from a config value, not a model. The peak is real rather than circular: if
this were only "the ground truth is all famous games", recall would keep
climbing as the corpus shrank, and instead it *falls* at 50,000, where the
filter starts eating expected games (Coffee Talk, A Short Hike, Monster Train).

**Not adopting threshold 10,000.** 57% costs 128,949 of 130,651 games — that is
search over the Steam top 1,700, and the long tail is the product. The finding
is that ranking needs a popularity *term*, not a cliff. That is a ranking change
and goes through plan mode.

Also worth keeping: latency falls with the threshold (280ms -> 55ms) because a
smaller candidate set is less work, the opposite of the HNSW `strict_order`
tradeoff, which pays *more* as the filter gets more selective.

---

## 2026-08-29 — A search took 18 seconds, and none of it was searching

**What broke.** `search.py "co-op base builder" --platform linux --max-price 20
--multiplayer` reported `embed 18017ms | query 417ms`. Filtering and ranking
were fine; embedding one short string took eighteen seconds.

**What I tried.** `ollama ps` showed the model resident with
`UNTIL: 4 minutes from now`. Ollama's default `keep_alive` is 5 minutes, after
which it evicts the model from VRAM. An idle CLI therefore pays a cold model
load — ~18s — to do ~20ms of arithmetic. Nothing was wrong with the code.

**What fixed it.** Pass `keep_alive` in the `/api/embed` body, from a new
`OLLAMA_KEEP_ALIVE` setting defaulting to `30m`. The model is 323MB against
16GB of VRAM, so holding it is free in practice. Set `-1` never to unload.

Worth remembering when the API arrives: a server that is idle overnight pays
this on its first request of the morning. Warming the model at startup, or
`-1`, is the fix there.

Separately, that run measured `query 417ms` against the 19.6ms I had estimated
while planning. The estimate used an existing row's embedding as the probe
vector, whose neighbours already matched the filters. A real query vector lands
in sparser space and iterative_scan works harder. The planning number was
optimistic by 20x — benchmark with real queries.

## 2026-08-26 — HNSW index build failed with "No space left on device"

**What broke.** `alembic upgrade head` on migration 0003 died with
`DiskFull: could not resize shared memory segment to 2144407040 bytes`. The
host had 588GB free.

**What I tried.** Read the byte count: 2,144,407,040 is exactly the 2GB set as
`maintenance_work_mem`. Not disk at all — Docker gives a container 64MB of
`/dev/shm` by default, and Postgres coordinates parallel workers through shared
memory. `max_parallel_maintenance_workers = 4` made pgvector build the graph in
a shared segment sized to maintenance_work_mem, which blew past 64MB. It
surfaces as a disk error because /dev/shm is a filesystem.

**What fixed it.** `shm_size: 4gb` on the db service in docker-compose.yml, then
recreate the container. It is a ceiling rather than a reservation, so nothing is
consumed until needed. Serialising the build with
`max_parallel_maintenance_workers = 0` also avoids it, but is slower and leaves
the same trap waiting for the next parallel operation.

DDL is transactional in Postgres, so the failed CREATE INDEX rolled back
cleanly and alembic_version stayed at 0002.

## 2026-08-26 — Every price in the database was a sale price

**What broke.** Spot-checking the fresh ingest against Steam, Stardew Valley
read $8.99 where its real price is $14.99. Not a parsing error — the column
held exactly what the source said.

**What I tried.** Checked the source record and found a `discount` field the
loader had dropped: `price=8.99, discount=40`. So `price` is the price on the
day of the scrape, and the scrape caught a Steam sale. 41,712 of 110,709 paid
games (37.7%) were discounted. Filtering "under $20" on it wrongly admitted
3,004 games — Rust reads as $19.99 and actually costs $39.99.

**What fixed it.** Migration `0002` adds `discount_pct` plus a generated
`list_price_usd` reversing the discount, guarded at both ends (0 means no sale;
100 would divide by zero, and 6 games are at 100%). Loader coerces `discount`,
which the source stores as str for 102,759 records and int for 36,205. Search
filters on `list_price_usd`.

Two things worth remembering. Derived list price is a cent low — Steam rounds
sale prices down, so $14.99 at -40% stores as $8.99 and reverses to $14.98;
fine for filtering, don't display it as exact. And when verifying the fix,
Python and Postgres disagreed on one row: Tomb Raider GOTY at $2.00 / -90%.
Python's float gave 20.000000000000004 and excluded it; Postgres' numeric gave
exactly 20.00. Postgres was right. That is what `numeric(10,2)` is for.

## 2026-08-20 — Embeddings ran at 0.5/sec on a 4080 SUPER

**What broke.** First smoke test of Ollama embeddings took 2.1s per call.
Projected out to 71 hours for 120k games. Expected minutes, not days.

**What I tried.** Checked `ollama ps` first, assuming CPU fallback — it said
`100% GPU`, so compute wasn't the problem. That meant the time was going into
per-request overhead. Benchmarked four combinations: `localhost` vs `127.0.0.1`,
fresh connection per call vs one reused `httpx.Client`.

**What fixed it.** `localhost` on Windows resolves to IPv6 `::1` first. Ollama
listens on IPv4 only, so every new connection stalls ~2.1s before falling back
to `127.0.0.1`. Using the IP directly: 0.5 -> 17.6/sec. Reusing one client:
35.7/sec. Batching via `/api/embed` with a list input: 62.3/sec. 125x total,
no hardware change. 120k games is now ~32 min.

Consequences: `.env.example` uses `127.0.0.1` for both Ollama and Postgres —
psycopg would hit the identical stall. The embedding client must hold one
long-lived `httpx.Client` and send batches, not one text per request.
