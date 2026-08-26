# Notes

What broke, what I tried, what fixed it. Newest first.

---

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
