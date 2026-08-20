# Vibe Search for Steam — Build Plan

A semantic search engine for Steam games. You describe the *feeling* of the game
you want in plain language — English or German — and it finds matches, filters
them on hard constraints, and explains why each one fits.

Three weekends. Each one ends with something that runs.

---

## The pitch, in one sentence

> "Co-op base builder under €20, relaxing not stressful, runs on Linux"

Steam's own search cannot answer this. Keyword matching fails on "relaxing",
and the price/platform constraints need real filtering, not similarity. This
project does both: a local LLM parses the query into structured filters plus a
semantic remainder, SQL handles the filters, pgvector handles the vibe.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12 + FastAPI | Job requirement |
| DB | PostgreSQL 16 + pgvector | Job requirement; one DB for rows *and* vectors |
| Frontend | React + Vite | Job requirement |
| Local LLM | Ollama | Job requirement (they run Ollama/vLLM on-prem) |
| Embeddings | `nomic-embed-text`, then `bge-m3` | Two models on purpose — see Weekend 3 |
| Validation | Pydantic v2 | The structured-extraction piece |
| Packaging | Docker Compose | Job requirement |
| Migrations | Alembic | Shows you think about schema change |

No LangChain, no LlamaIndex, no agent framework. Everything here is a direct
API call. Frameworks would hide exactly the parts you want to be able to
explain in an interview.

---

## Repo layout

```
steam-vibe/
├── CLAUDE.md                  # context for Claude Code — write this first
├── BUILD_PLAN.md              # this file
├── README.md                  # written last, contains your eval numbers
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── pyproject.toml
│   ├── alembic/
│   ├── app/
│   │   ├── main.py            # FastAPI app + routes
│   │   ├── config.py          # pydantic-settings
│   │   ├── db.py              # engine, session
│   │   ├── models.py          # SQLAlchemy tables
│   │   ├── schemas.py         # Pydantic request/response + ParsedQuery
│   │   ├── embedding.py       # Ollama embedding client
│   │   ├── llm.py             # Ollama chat client
│   │   ├── query_parser.py    # natural language -> ParsedQuery
│   │   └── search.py          # hybrid filter + vector search
│   ├── ingest/
│   │   ├── load_kaggle.py     # CSV -> postgres
│   │   ├── enrich_steam.py    # storefront API for German text
│   │   └── embed_all.py       # batch embedding job
│   └── eval/
│       ├── queries.yaml       # your 30 labelled test queries
│       └── run_eval.py        # recall@10, prints a table
└── frontend/
    ├── package.json
    └── src/
        ├── App.tsx
        ├── api.ts
        └── components/
```

---

## Write CLAUDE.md before anything else

This is the single highest-leverage thing you do. Claude Code reads it on every
session. Put in it:

```markdown
# Steam Vibe Search

## What this is
Semantic search over ~120k Steam games. Natural-language query in, ranked
games out. Hybrid: structured SQL filters + pgvector similarity.

## Rules
- Python 3.12, type hints on every function signature.
- Pydantic v2 for all boundaries (API in/out, LLM output parsing).
- SQLAlchemy 2.0 style (`select()`, not legacy Query).
- No LangChain, no LlamaIndex, no agent frameworks. Direct HTTP to Ollama.
- All DB schema changes go through Alembic migrations. Never edit tables
  by hand.
- Every ingest script must be idempotent and resumable — I will interrupt
  them.
- Errors from the LLM are expected, not exceptional. Parse failures must
  degrade to pure semantic search, never 500.

## Commands
- `docker compose up -d db ollama` — start deps
- `cd backend && uv run uvicorn app.main:app --reload`
- `cd backend && uv run python -m eval.run_eval`
- `cd frontend && npm run dev`

## Current state
(update this at the end of every session)
```

That last section matters. Update it when you stop working. Future sessions
pick up instantly instead of re-deriving the whole project.

---

# Weekend 1 — Data in, search working from the command line

**Goal:** you can type a sentence into a terminal and get back sensible games.
No API, no UI. If this weekend works, the project works.

## Saturday morning: get the data

Use the Kaggle Steam Games Dataset (Martin Bustos / fronkongames) as your
primary source. It has ~120k games with descriptions, tags, genres, price,
platforms, and review counts, already scraped. Do **not** try to crawl Steam
from scratch — scraping all of Steam takes several days at the storefront
API's tolerable request rate, and that is not what this weekend is for.

Known trap in that CSV: the header has a column-merge bug where
`DiscountDLC count` is actually two columns, so everything from index 8
onward is offset by one. Read raw rows with corrected indices rather than
trusting `pandas.read_csv` headers. Verify by spot-checking five games you
know against their Steam pages.

**Schema** — start here, migrate later:

```sql
games (
  app_id            integer primary key,
  name              text not null,
  short_description text,
  detailed_description text,
  release_date      date,
  price_eur         numeric(10,2),
  is_free           boolean,
  windows/mac/linux boolean,
  positive_reviews  integer,
  negative_reviews  integer,
  embedding         vector(768)
)
game_tags (app_id, tag, votes)     -- normalised, indexed
game_genres (app_id, genre)
```

Index: btree on `price_eur`, `release_date`; GIN on the tag join; HNSW on
`embedding` (add the vector index *after* bulk insert, not before — building
it incrementally on 120k rows is dramatically slower).

## Saturday afternoon: embeddings

```bash
ollama pull nomic-embed-text
```

Build the text you embed carefully — this decides everything downstream.
Don't just embed the description. Concatenate:

```
{name}. {short_description} Tags: {top 15 tags by votes}.
```

The tags are the highest-signal part. Steam's community tags are effectively
crowd-sourced vibe labels ("Relaxing", "Atmospheric", "Great Soundtrack") and
they're doing most of the work in this project.

Batch the embedding job, checkpoint progress to the DB, and make it resumable.
120k games will take a while on a laptop. Start it and go do something else.

## Sunday: search from the CLI

Write a throwaway script that takes a string, embeds it, and runs:

```sql
SELECT name, short_description, 1 - (embedding <=> :q) AS score
FROM games
WHERE positive_reviews + negative_reviews > 50
ORDER BY embedding <=> :q
LIMIT 10;
```

Note the review filter — without it your results fill with abandoned asset
flips that nobody has played. This is the kind of unglamorous fix that
actually determines whether the thing feels good.

Then spend an hour just *playing with it*. Type in twenty vibes. This is the
fun part and it's also research — you'll notice the failure modes yourself.

**Write down the queries where it fails.** Those become your eval set.

### Weekend 1 done when
You can run `python search.py "cozy farming game with fishing"` and the
results make you nod.

---

# Weekend 2 — Structured extraction, API, UI

**Goal:** the query box understands constraints, and there's a web page.

## Saturday morning: the query parser

This is the piece that maps directly onto the job description, so give it real
attention.

Define the target schema first:

```python
class ParsedQuery(BaseModel):
    semantic_query: str          # the vibe part, goes to embeddings
    max_price_eur: float | None = None
    min_price_eur: float | None = None
    required_tags: list[str] = []
    excluded_tags: list[str] = []
    platforms: list[Literal["windows", "mac", "linux"]] = []
    released_after: int | None = None   # year
    multiplayer: bool | None = None
```

Then prompt a local chat model (`llama3.1:8b` or `qwen2.5:7b` are both fine
here) to fill it from the raw query. Ask for JSON only, parse it into the
Pydantic model, and — critically — **handle failure**:

- Model returns malformed JSON → fall back to pure semantic search on the
  raw string. Log it. Never 500.
- Model invents a tag that doesn't exist in `game_tags` → drop that tag
  rather than returning zero results. Fuzzy-match it against your real tag
  vocabulary first.

That second one is the interesting bug. The model will confidently produce
`"Cozy"` when Steam's actual tag is `"Relaxing"`. Fix it by passing your
top ~200 real tags into the prompt as the allowed vocabulary. That's a real
grounding technique and a good interview anecdote.

## Saturday afternoon: FastAPI

Two endpoints is enough:

```
POST /api/search   { "query": "..." } -> { parsed: ParsedQuery, results: [...] }
GET  /api/game/{app_id}
```

Return the parsed query alongside the results. The frontend will show it, and
it's the single best UI decision in this project — see below.

Search logic: apply the structured filters as a SQL `WHERE`, then order by
vector distance on `semantic_query`'s embedding within that filtered set.

## Sunday: React frontend

Keep it to one page. A search box, a list of result cards, and one thing that
makes this project feel considered:

**Show the parsed query as editable chips above the results.** The user types
"cheap co-op games for Linux" and sees `≤ €20` `co-op` `Linux` appear as
removable pills. Click the X on one and results update.

This is worth doing because it makes the AI's interpretation *visible and
correctable* rather than hidden. It's the UI expression of the exact principle
in the job posting — you can see what the model decided and override it. It
also turns the parser's mistakes from silent failures into an obvious, fixable
thing, which is the whole game.

### Weekend 2 done when
You can type a mixed vibe+constraint query into a browser and watch the chips
appear and the results filter.

---

# Weekend 3 — German, evaluation, packaging

**Goal:** turn a demo into something with numbers attached.

## Saturday morning: German

Two parts.

**German queries.** Test what happens now: *"gemütliches Aufbauspiel für zwei,
nichts Stressiges."* With `nomic-embed-text` (English-centric) this will
degrade noticeably. That degradation is the point — measure it before you fix
it.

**German descriptions.** The Steam storefront API takes a language parameter:

```
https://store.steampowered.com/api/appdetails?appids=1145360&l=german
```

No key needed, but it's rate-limited and unofficial — throttle it hard
(roughly 200 requests per 5 minutes is the commonly cited safe rate), and
only fetch for the top few thousand games by review count. You do not need
all 120k.

Then swap to a genuinely multilingual model:

```bash
ollama pull bge-m3
```

BGE-M3 covers 100+ languages and is MIT-licensed, which makes it the sane
self-hosted default. Re-embed, re-run your eval, and record both numbers.

**Watch for this specifically:** German compound nouns. "Aufbauspiel",
"Koop-Modus", "Rundenbasiert" are single semantic units that English-tuned
tokenizers shred into fragments. You will see it in your retrieval scores.
Note down concrete examples — this is precisely the kind of thing Trasenix
deals with in German documents, and having seen it firsthand is worth more
than having read about it.

## Saturday afternoon: the eval harness

This is the part that turns a toy into a portfolio piece. Budget real time
for it.

`eval/queries.yaml`:

```yaml
- query: "cozy farming sim with fishing"
  expect: [413150, 1229490, 1997350]     # app_ids that MUST appear in top 10
- query: "co-op base builder under 20 euros"
  expect: [892970, 526870]
- query: "gemütliches Aufbauspiel für zwei"
  expect: [892970, 1621690]
```

Aim for 30 queries. Include the failures you noted in Weekend 1. Include at
least 8 German ones. You don't need a "complete" ground truth — you need
games you're confident *should* be in the top 10.

`run_eval.py` computes recall@10 and prints a table. Then run it against every
variation you've built:

| Config | recall@10 (EN) | recall@10 (DE) |
|---|---|---|
| nomic-embed-text, semantic only | | |
| nomic-embed-text, hybrid | | |
| bge-m3, hybrid | | |
| bge-m3, hybrid, tags in embedded text | | |

That table is the most valuable artifact in the whole project. It is the
difference between "I built a search engine" and "I built a search engine and
here is what actually moved the numbers."

## Sunday: Docker + README

`docker-compose.yml` with four services: `db` (pgvector image), `ollama`,
`backend`, `frontend`. One `docker compose up` should bring the whole thing
to life on a clean machine. Test that by actually deleting your volumes and
running it.

README structure:

1. One-line pitch + a GIF of it working
2. Quickstart (three commands, no more)
3. Architecture — one diagram, be honest about what's simple
4. **The results table**
5. **What doesn't work well** — be specific. Under-reviewed games, ambiguous
   tags, German queries about mechanics rather than mood, whatever you found.

That last section is counterintuitive and it's the one that gets you hired.
Nearly every portfolio README oversells. A section that says "here's where it
falls over and why" reads as engineering maturity, and it's exactly what
point 4 of their priority list is asking for.

---

# Working with Claude Code on this

## Use plan mode for anything structural

Before schema design, the query parser, and the search logic, get a plan out
first and read it properly. These three decisions constrain everything after
them. The rest — CRUD endpoints, React components, the ingest scripts — you
can let it write directly.

## Review discipline, calibrated

The job posting draws exactly this distinction, so practice it deliberately:

- **Ingest scripts, eval harness, throwaway CLI** — if it runs, it's fine.
- **Schema, migrations, query parser, search ranking** — read every line.
  A silently wrong `ORDER BY` produces plausible results forever.

Write which category you're in at the top of each session. It'll make you
conscious of a judgement you're otherwise making by accident.

## Things to explicitly stop it doing

- Adding LangChain "to simplify the LLM calls"
- Wrapping the search in an agent loop
- Writing a `try: except: pass` around LLM parsing (you want the fallback
  *and* the log)
- Creating tables without a migration
- Adding a `requirements.txt` alongside `pyproject.toml`

Put these in CLAUDE.md the first time each one happens.

## Commit per feature, not per session

You want a git history that shows the project's evolution. `git log` is a
legitimate part of your portfolio here — it demonstrates working style.

## Keep the log

Open `NOTES.md` on day one. Every time something breaks, write three lines:
what broke, what you tried, what fixed it.

You are going to need this. The application asks you to write about which
tools you used, what you built, where the problems were, and how you solved
them. Reconstructing that from memory three weeks later produces something
vague. A file with fifteen real entries produces something specific, and
specific is the entire point.

---

# What goes on the CV afterwards

> **Steam Vibe Search** — semantic game discovery over 120k titles
> - Hybrid search combining pgvector similarity with SQL constraint
>   filtering; natural-language queries parsed into validated Pydantic
>   schemas by a locally hosted LLM (Ollama), with graceful degradation
>   on parse failure.
> - FastAPI + React + PostgreSQL, deployed via Docker Compose; fully
>   self-hosted with no external AI API dependency.
> - Built a 30-query labelled evaluation set covering English and German;
>   improved recall@10 from X% to Y% by switching embedding models and
>   incorporating community tags into the embedded text.
> - Developed with Claude Code throughout; [the specific thing you
>   overruled it on].

Fill the brackets with true numbers. If recall only went from 61% to 68%,
write 61% to 68% — a modest honest number is worth more than an impressive
vague claim, and anyone competent will ask you follow-up questions you can
actually answer.

---

# Weekend 4 (optional) — depth, not features

Only start this once Weekend 3 is finished and the eval table has real numbers
in it. Each of these adds a row to that table. That's the test for whether an
addition is worth it: **if it doesn't produce a number, it's decoration.**

Ordered by value per hour.

## 1. Add a reranking stage (half a day, biggest single win)

Right now you retrieve 10 results by vector distance and show them. Production
RAG doesn't do that. It over-retrieves, then reranks with a cross-encoder.

```bash
ollama pull bge-reranker-v2-m3   # or run it via sentence-transformers
```

Pipeline becomes: filter in SQL → retrieve top 50 by vector → cross-encoder
scores each (query, game) pair properly → return top 10.

The difference: a bi-encoder embeds the query and the game separately and
compares vectors, so it never actually *reads them together*. A cross-encoder
sees both at once and can catch "co-op" meaning local co-op vs. online. It's
much slower, which is exactly why you only run it on 50 candidates instead of
120,000.

Expect a real jump in recall@10. This is the single most standard thing
missing from your current design, and being able to explain why two-stage
retrieval exists is a strong interview answer.

## 2. Hybrid sparse + dense retrieval (half a day)

Pure semantic search fails badly on proper nouns. Search "Factorio-like
automation" and vector similarity may not surface the actual Factorio clones,
because "Factorio" as a token carries little semantic weight.

Postgres already has full-text search built in — no new infrastructure:

```sql
ALTER TABLE games ADD COLUMN ts tsvector
  GENERATED ALWAYS AS (to_tsvector('english', name || ' ' || short_description))
  STORED;
CREATE INDEX ON games USING GIN(ts);
```

Run BM25-ish keyword search and vector search in parallel, then merge with
Reciprocal Rank Fusion:

```python
score(doc) = sum(1 / (60 + rank_in_list) for each list containing doc)
```

RRF is about six lines of code and needs no tuning, which is why it's the
default fusion method in most production stacks. Add German to the tsvector
config (`to_tsvector('german', ...)`) and Postgres will handle German stemming
for you — worth measuring against the English config on your German queries.

Two new eval rows: sparse only, and fused.

## 3. Put the eval in CI (two hours, disproportionate signal)

GitHub Actions workflow that runs `eval/run_eval.py` on every PR and **fails
the build if recall@10 drops more than 2 points** from the value stored in
`eval/baseline.json`.

This is small, and it's the thing I'd point at hardest in an interview. You
already built CI/CD pipelines at Decubate — this connects that experience
directly to AI work, and it's the concrete engineering answer to their
"a wrong result that looks right costs us money" priority. Most people
treat model quality as vibes. A red X on a pull request because retrieval
regressed is a different level of seriousness.

Bonus: it forces you to keep the eval set maintained, which you otherwise
won't.

## 4. Grounded explanations with a hallucination check (half a day)

You planned a one-line "why this matches" from the local model. Make it
honest:

- The model may only reference tags and phrases that actually appear in that
  game's record. Pass them in explicitly and say so in the prompt.
- After generation, verify: extract the tags the explanation claims, check
  each against `game_tags` for that app_id. If it cited a tag the game
  doesn't have, discard the explanation and fall back to listing the matched
  tags plainly.
- Count how often that check fires and put the number in your README.

"My explanation layer hallucinated a tag in 4% of cases so I added a
post-generation verifier" is a far better sentence than anything about how
well the feature works.

## 5. Query-relaxation loop (a few hours)

When the structured filters produce fewer than N results, don't return an
empty page. Relax the least-important constraint and retry, then tell the
user what you dropped: *"No results under €10 — showing results under €20."*

This is genuinely agentic behaviour — a decision loop with a stopping
condition — implemented in about forty lines with no framework. That's the
right way to demonstrate you understand agents: by not needing one.

## 6. Observability (two hours)

Log per-request: parse latency, embed latency, retrieval latency, rerank
latency, result count, whether fallbacks fired. Expose `/api/stats` with p50
and p95. Put a small panel in the UI.

Cheap, and it makes the reranker's cost visible — you'll be able to say "the
cross-encoder added 180ms at p95 and 9 points of recall, and here's why I
thought that trade was worth it."

## Still not worth adding

- **An agent framework.** Item 5 covers the concept honestly. Bolting on
  LangGraph would make the codebase harder to explain, not more impressive.
- **Fine-tuning an embedding model.** Days of work, and with 30 labelled
  queries you'd be overfitting to your own test set — which you'd then have
  to disclose, at which point the number means nothing.
- **User accounts, saved searches, deployment.** Still no.
- **A second data source.** The project is about retrieval quality, not
  ingestion breadth.

# Weekend 5 (optional) — discount likelihood, done honestly

The feature: alongside each result, an indication of whether it's worth
waiting. *"Last discounted 112 days ago, typically goes on sale every ~90
days at -50%. Autumn Sale starts in 9 days."*

This is the highest-risk addition in the whole plan, and the highest-reward
if you do it properly. Read this whole section before starting.

## The trap

A search result that's mediocre looks mediocre. A probability that's wrong
looks authoritative. If you show "73% chance of a discount next week" and
that number came from an unvalidated model, you have built exactly the
failure the job posting warns about — a wrong result that looks right.

So the deliverable here is not a model. It's a **calibration curve**.

## Reframe: this is a base-rate problem, not an ML problem

Steam discounts are highly structured:

- Seasonal sales (Spring / Summer / Autumn / Winter) at roughly known dates
- Publisher-specific cadences
- Steam's own rules about minimum intervals between promotions and
  discount depth relative to the last one
- Age of the game (new releases discount rarely; five-year-old games
  discount constantly)

SteamDB's FAQ makes the point plainly: a game that appeared in every previous
major sale will almost certainly appear in the next one. That's a base rate,
and it will be hard to beat.

**Build the heuristic baseline first.** Then build a model. Then show that
the model beats the baseline by 3 points, or doesn't. Either result is a good
README section; only the pretence of sophistication is bad.

## Data

**IsThereAnyDeal API** (`docs.isthereanydeal.com`) — the legitimate source.
Register an app to get a key at `isthereanydeal.com/dev/app/`. Gives you
lookups by Steam appid, current deals across stores, and historical price
data including store lows and price history.

Do **not** scrape SteamDB. Their terms prohibit it and they actively block it.
This matters beyond politeness: "I checked whether I was allowed to use this
data source" is a thing a company handling investor-facing data will care
about.

**Also: start logging prices yourself on day one of Weekend 1.** A tiny cron
job hitting the storefront API for your top few thousand games, appending to
a `price_history` table. By the time you reach this weekend you'll have
several weeks of your own series — not enough for seasonality, but enough to
sanity-check ITAD's data against reality. Discovering a discrepancy between
two sources and investigating it is a strong interview story, and you can
only have it if you started collecting early.

```sql
price_history (
  app_id       integer references games,
  observed_at  timestamptz,
  price_eur    numeric(10,2),
  discount_pct integer,
  primary key (app_id, observed_at)
)
```

## Features (all boring on purpose)

```
days_since_last_discount
discount_count_last_365d
median_discount_depth
median_days_between_discounts
days_since_release
days_until_next_known_steam_sale
is_currently_discounted
publisher_discount_frequency
review_count            # proxy for popularity
base_price_bucket
```

Target: *did this game go on discount in the next 7 days?*

Model: logistic regression first, then gradient boosting (`scikit-learn` or
`lightgbm`). Nothing deeper. This also, incidentally, puts a real project
behind the Scikit-learn line that's currently sitting unsupported on your CV.

## The part that actually matters

### Temporal splits, not random splits

Train on everything before date X, test on everything after. A random split
leaks future information into training — you'd learn about the Winter Sale
from held-out rows in the same week — and produces a beautiful, meaningless
accuracy score. Getting this wrong is the single most common mistake in
time-series ML and getting it right is worth saying out loud.

### Calibration, not accuracy

Accuracy is the wrong metric. What matters is: **when the model says 70%,
does it happen 70% of the time?**

- Compute a Brier score
- Plot a reliability curve: predicted probability bucket on x, observed
  frequency on y. Perfect calibration is the diagonal.
- If it's off, apply Platt scaling or isotonic regression
  (`sklearn.calibration.CalibratedClassifierCV`) and plot it again

Put both curves in the README, before and after. That plot is the single
most impressive artifact you could put in front of this employer. It says:
I don't just produce numbers, I check whether my numbers mean anything.

### Compare against the baseline honestly

| Model | Brier score | Calibration error |
|---|---|---|
| Always predict base rate | | |
| Heuristic (days since last discount + sale calendar) | | |
| Logistic regression | | |
| Gradient boosting | | |
| Gradient boosting, calibrated | | |

If the heuristic wins, say so in the README. That's not a failed project —
that's a finding, and reporting it is worth more than a model that "wins" by
a leaked split.

## Displaying it

**Do not show a bare percentage.** Show the evidence, then the estimate:

```
Discounted 6 times in the last year, typically -50%
Last discount: 112 days ago (usual gap: ~90 days)
Autumn Sale starts in 9 days
→ Likely to drop soon
```

Use coarse language buckets — "likely soon", "unlikely soon", "not enough
history" — rather than a false-precision number. And add a genuine
"not enough data" state for games with fewer than N observed price points,
rather than letting the model extrapolate from nothing.

That last one is the whole ethic of this feature: knowing when to say
nothing.

## Scope warning

This is a real ML project bolted onto a search project. It can easily eat
three weekends and become the whole thing. Guardrails:

- Only build it after Weekends 1–3 are done and the README exists
- Timebox the modelling to one weekend; if the calibration plot isn't
  produced by Sunday evening, ship the heuristic alone and say so
- The heuristic version alone is a perfectly good feature. The model is
  the optional part, not the reverse

# Scope discipline

Things that will tempt you and should be cut:

- User accounts and saved searches
- A recommendation engine ("games like this one")
- Scraping reviews for sentiment
- Fine-tuning an embedding model
- Deploying it publicly

None of these improve the CV entry. All of them cost a weekend. If you finish
early, spend the time on the eval set instead — more labelled queries make
every number in your README more credible.