# Refreshing the data

Run these from `backend/` when `data/games.json` is replaced with a newer
Kaggle dump. Nothing here is destructive; every step is safe to interrupt and
re-run.

## 1. Replace the file

```bash
# from the repo root
mv ~/Downloads/games.json data/games.json
```

Optional but cheap insurance before a large refresh:

```bash
docker compose exec -T db pg_dump -U steam steamvibe > backup.sql
```

## 2. Make sure the database is up

```bash
docker compose up -d db
```

The container stops whenever Docker Desktop restarts. The data lives in the
`pgdata` volume, so nothing is lost — but the loader will fail with a
connection timeout if you skip this.

## 3. Apply any pending migrations

```bash
uv run alembic upgrade head
```

No-op if the schema hasn't changed. Cheaper than debugging a column that
doesn't exist yet.

## 4. Drop the HNSW index, then reload

```bash
uv run alembic downgrade 0004     # drops ix_games_embedding_hnsw
uv run python -m ingest.load_games --reload
```

**Drop the index first.** It is 510MB over 130,651 vectors, and a `--reload`
updates every one of the 138,964 rows. Each update needs a new entry in every
index on the table, and HNSW insertion is deliberately expensive — the same
reason it is built after the embed job rather than before. With the index in
place a reload runs for many minutes; without it, about three. Step 6 rebuilds
it.

**`--reload` is required.** Without it the loader skips every app_id already
present and only picks up genuinely new games — so changed prices, review
counts and descriptions would be silently ignored.

Takes about 3 minutes for 139k games. One transaction per 1,000-game batch, so
an interrupt costs at most one batch.

What the upsert does with embeddings:

| `embed_text` | Result |
|---|---|
| unchanged | embedding kept — no re-embedding cost |
| changed | embedding, model and timestamp set to NULL |

That second case matters. A vector built from text that no longer exists is
worse than no vector, because the embed job skips non-NULL rows and would never
revisit it. Nulling puts the row straight back in the queue.

## 5. See what needs re-embedding

```sql
SELECT count(*) AS awaiting_embedding
FROM games
WHERE embed_text IS NOT NULL AND embedding IS NULL;
```

New games plus any whose text changed.

## 6. Re-embed, then rebuild the vector index

```bash
uv run python -m ingest.embed_all
```

Then put HNSW back, now that all the heavy writing is done:

```bash
uv run alembic upgrade head
```

The embed job only touches rows where `embedding IS NULL`, so it costs
proportional to what actually changed, not the full 12 minutes. Interruptible
— re-run to continue.

## 7. Verify

```bash
docker compose exec -T db psql -U steam -d steamvibe -f - < ingest/verify_load.sql
```

**Expect the row-count checks to FAIL after a refresh.** That is the correct
signal: the data changed. Read the new numbers, confirm they're plausible
(more games, not fewer; similar tag-per-game ratio), then update the `expected`
values in `verify_load.sql` and commit that as part of the refresh.

The other checks — unparsed dates, null prices, list price below sale price,
orphaned rows, date range — are invariants. If one of those fails, the code is
wrong, not the data.

## 8. Spot-check against Steam

```bash
docker compose exec -T db psql -U steam -d steamvibe -f - < ingest/spot_check.sql
```

Open the `steam_url` values and compare. This is the only check that catches a
wrong *assumption* rather than a wrong *parse* — every automated check in
`verify_load.sql` passed while every price in the database was a sale price.
See NOTES.md, 2026-08-26.

## 9. Refresh planner statistics

```sql
VACUUM ANALYZE games;
```

After changing a large fraction of the table, Postgres' row estimates are
stale, and it may pick a bad plan for search queries.

The HNSW index is rebuilt in step 6, not here. It does maintain itself on
insert and update, but that maintenance is what makes a full reload slow, which
is why step 4 drops it. A change of embedding model, and therefore of vector
dimension, needs a new index definition and its own migration.
