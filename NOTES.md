# Notes

What broke, what I tried, what fixed it. Newest first.

---

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
