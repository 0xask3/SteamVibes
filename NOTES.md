# Notes

What broke, what I tried, what fixed it. Newest first.

---

## 2026-09-19 - 68.8% was one draw from a non-deterministic index build

**What broke.** The real database scored 67.1% overall against the README's
68.8%, with no change to the recall path.

**What I tried.** Ruled out everything else with a test each: a re-embed of 256
rows was bit-identical to the stored vectors across the Ollama upgrade; pgvector,
the libraries and the code were unchanged; batch size and VRAM changed nothing.
Then copied the database and rebuilt the index twice through 0007: 67.9% and
66.8%. The parallel build is not deterministic, and last session's accidental
rebuild had drawn a worse graph.

**What fixed it.** `HNSW_EF_SEARCH` 200 -> 800, from a sweep of 200-1000 over
three independent builds: 800 is the smallest value where they agree on every
query of both sets and match 1000. Not 600, which scored 0.3 higher only because
it was still approximate on one query. EN +4.4% [+1.0, +8.5], `tail` +9.1%, DE
+2.5%; filtered SQL median 16 -> 23ms. failures.md #42 has the table.

## 2026-09-19 - The reranker's batch default filled the card on its own

**What broke.** A fresh-clone `run_eval` only reproduced the README's latency
after `ollama stop qwen3.5:9b`. `RERANK_BATCH_SIZE=128` was never measured and
peaked the whole 16GB card at 15.9GB, spilling to rerank p95 8-9s.

**What I tried.** Probed 128/64/32/16 on 30 queries in both orders, then full EN
and DE runs at 128 and 32 with the chat model pinned resident. Smaller batches
were FASTER, not slower - most likely padding, since every batch is padded to its
longest pair. But 16 moved the top-10 on 13 of 30 queries, and a top-10 id
comparison over all 236 found 32 reordering near-ties inside 4 of them; the
30-query probe had called that "identical". Each size is deterministic run to
run, so it is fp16 arithmetic, not noise.

**What fixed it.** Default 32: same top-10 SETS on all 236 queries, so recall is
identical (`compare_runs` 0W 0L, 118 ties each). Rerank median 1,033 -> 763ms,
p95 6,747 -> 886ms, peak 15.9 -> 13.4GB. The batch is no longer described
anywhere as speed-only.

## 2026-09-19 - The first search after every restart paid for the reranker

**What broke.** On a fresh clone run from source, the first search took 124s,
122s of it "reranking", while the UI promised about 20. The cross-encoder loads
lazily, and on a new machine that includes a 2.4GB download. Even cached, every
restart made its first search pay 8,822ms of load plus warm-up.

**What I tried.** Looked for why `_warm_models()`, which exists precisely to keep
cold loads off the first user, did not cover it. It warms the embedder and the
chat model, and was written before stage 3 existed.

**What fixed it.** It now warms the cross-encoder too, through the same
`rerank_scores()` a search uses, only under `RANK_METHOD=rerank` and in its own
try so an Ollama failure cannot skip it. Measured: warm in 8.2s at boot, first
search 2.5s. With an empty Hub cache health still answers in 4ms during the
download, and a search sent mid-download waits on the loader's lock - one load,
not two.

## 2026-09-19 - The container cached the tags of an empty database

**What broke.** A fresh-clone run found the containerised API extracting NO tags
after ingest. `get_tag_vocabulary()` was an `lru_cache`, `docker compose up`
starts the API before ingest, and the startup warmup parses a query - so it
cached `()` for the life of the process. The explanation verifier reads the same
list, so that went blind too. Nothing was logged.

**What I tried.** Proved the mechanism before touching code: restarting only the
backend container restored the README's tags exactly. Then measured whether the
cache was worth keeping (the read is 52ms against a ~1,200ms parse, so yes) and
found the narrower version of the same bug - 2,000 loaded games already carry 425
of the 452 tags, so a search during the load would pin a list missing 27.

**What fixed it.** An empty vocabulary is never cached and logs a WARNING; a
non-empty one expires after `VOCABULARY_TTL_S`. Verified against a scratch
database: empty -> no tags plus the warning; `load_games --limit 2000` with the
server still running; same query -> the exact documented tags, no restart.

## 2026-09-18 - The documented setup path ended in a SystemExit

**What broke.** README step 2 (`docker compose up -d`) runs `alembic upgrade
head`, and 0007 BUILDS the HNSW index - so a brand-new database arrives with the
index present. Step 3's `embed_all` then refuses to start, correctly, because
writing 130k vectors with it in place rebuilds the graph row by row. The
documented happy path had been wrong since the compose services were added.

**What I tried.** Proved it rather than read it: cloned to a temp dir, brought up
an isolated Postgres on 5433 under a separate compose project, confirmed the
index exists on an empty database, then ran the corrected sequence end to end -
downgrade 0006, load_games (3m08s, exact documented counts), embed_all, upgrade
head.

**What fixed it.** The two alembic lines are in README step 3 with the refusal
quoted, plus "stop at 0006, never lower". CLAUDE.md's summary carries them too.

**And a mistake of my own, worth more than the bug.** I wrote `cd backend &&
export DATABASE_URL=...` from a directory already named `backend`. The `cd`
failed, `&&` short-circuited, the export never ran, and the `alembic downgrade
0006` that followed hit the REAL database. It dropped the production index.
Recovered with `upgrade head` because 0007's downgrade only drops an index; one
revision lower would have destroyed the corpus. Set the variable on its own line,
and verify which database you are pointed at before any downgrade.

---

## 2026-09-08 - The refresh guide told you to delete every embedding

**What broke.** Nothing yet, which is the point - found by a cleanup pass.
`ingest/README.md` said `alembic downgrade 0004` to drop the HNSW index. The
comment is true and the command is a disaster: downgrading to 0004 runs 0006's
downgrade on the way, re-dimensioning `games.embedding` to 768 with
`USING NULL::vector(768)` and discarding all 130,651 vectors.

**What I tried.** Read the chain rather than the target: 0007 is the revision
that builds the CURRENT index, so the correct stopping point is 0006. CLAUDE.md
already said 0006; the ingest guide had drifted and nothing cross-checked them.

**What fixed it.** `downgrade 0006`, plus a paragraph saying what 0004 would have
done. The general lesson: a downgrade target names where you STOP, not what you
undo, so every revision in between runs - check what they do to DATA, because a
migration that is purely additive upward can be destructive downward.

## 2026-09-08 - The bigger German set answered the question, and the answer was no

**What broke.** Nothing. This closed the one deferred claim in the repo: the
cross-encoder appeared to move German `specific` off a 55.6% that two embedding
models had left identical, recorded as failing a paired test at n=9.

**What I tried.** Built `eval/queries_de.yaml` - the same 118 targets and tiers
asked in German, generated so the app_ids were copied rather than retyped, taking
`specific` German from 9 queries to 44. Then re-ran the comparison paired, with
the sign test and bootstrap that now live in `eval/paired.py`.

**What fixed it.** Nothing to fix; the claim is retired rather than deferred.
Reranking on German is +3.8% [-3.0%, +11.0%], p=0.263 (at ef_search 200; +4.7%,
p=0.189 at 800). At n=44 that is an informative negative rather than an
underpowered one. The same set also produced the first EN/DE number not
confounded by tier mix or target choice.

## 2026-09-08 - The recorded p-value was one-sided and nothing said so

**What broke.** Putting the sign test into `eval/paired.py` meant checking it
against a known result, and CLAUDE.md's only recorded one was "11 wins to 5,
p=0.105". My implementation returned 0.2101 for that exact split.

**What I tried.** Worked the binomial by hand: for 11 of 16 the upper tail is
6885/65536 = 0.1051, and twice that is 0.2101. Both are correct - the same data
under different conventions, and nothing in the repo said which.

**What fixed it.** Kept two-sided, because "are these different" is not a
directional hypothesis and choosing the direction after seeing which arm won is
what makes a one-sided test flattering. Labelled it in `paired.py`'s self-test,
which asserts the 0.2101 and prints both, and beside the recorded 0.105. No
published claim changes - but a future two-sided p compared against that 0.105
would have looked like a result appearing out of nowhere.

## 2026-09-08 - Thirty searches at once take 209 seconds each

**What broke.** A concurrency check on the metrics recorder - 30 parallel
searches, to prove the lock loses nothing - timed out at a 180s deadline that
budgeted 2.4s per request.

**What I tried.** Read the per-request log lines the same change had just added:
`209446ms (parse 70175, relax 4, embed 267, query 59, rerank 138657)`. The
requests were not stuck, they were queueing - one GPU holds Ollama's resident
6.6GB chat model and the in-process cross-encoder, and nothing bounds how many
requests contend for it.

**What fixed it.** Nothing, and that is the finding: the API is a single-user
system under load, now written down in README with a number rather than left to
be discovered. The recorder was fine - log lines and `search.n` agreed exactly.
Lesson for the check: a concurrency test on a component must not run through a
pipeline whose bottleneck is a shared GPU, or it measures the GPU.

## 2026-09-08 - The p95 guard that did nothing at exactly its own boundary

**What broke.** `/api/stats` withholds p95 until enough samples exist. I set the
threshold to 20 by eye, and the check script printed `n=20 p50=110.0 p95=119.0
max=119.0` - at the exact boundary the guard permitted the thing it forbids.

**What I tried.** Enumerated the nearest-rank index against n rather than
reasoning about it: `min(n-1, int(n*0.95))` equals `n-1` for every n up to 20,
because at n=20 it is `int(19.0)`. The first n where p95 stops being the maximum
is **21**.

**What fixed it.** `MIN_P95_SAMPLES = 21`, plus two assertions so it cannot
regress silently: `p95 < max` at the threshold, and a derived check recomputing
the boundary from the formula. What caught it was printing p95 beside max rather
than asserting p95 was merely non-null. A guard that has never been shown to fire
is not known to work.

## 2026-09-08 - Two identical eval runs, 20x apart on latency

**What broke.** `run_eval` reported median search 1,844ms and rerank p95
21,284ms against documented figures of 1,021ms and 1,376ms - which reads exactly
like a regression from the change just made.

**What I tried.** Checked recall first: byte-identical to the committed table. A
change that slowed reranking 20x without moving a single query is not a ranking
change. Then checked the GPU: 8.3GB in use, Ollama holding its 6.6GB chat model,
after I had restarted the API four times and reloaded the cross-encoder each
time.

**What fixed it.** Nothing in the code - re-running gave 1,169ms and 1,573ms.
Rerank latency measures the machine's VRAM state as much as the model, so a
figure taken right after other GPU work is not a measurement. The recall column
is what said the difference was environmental.

## 2026-09-08 - The parser costs more than the cross-encoder

**What broke.** Nothing - the whole project had treated the reranker as the
expensive stage, on the strength of it being the thing bought deliberately with
latency.

**What I tried.** 50 real searches through `/api/stats`, nothing excluded: parse
1,217ms p50, rerank 1,048ms, query 46ms, embed 31ms, relax 3ms.

**What fixed it.** A corrected belief. The dominant cost is the LLM parse, which
nobody classes as a ranking stage, and that makes the editable-chip path worth
more than "14x faster" conveyed: it removes the single largest stage. The
relaxation ladder also costs 3ms on live traffic rather than the 11-24ms its
design note claimed. Both were assertions before there was anything to check
them.

## 2026-09-08 - Relaxation: the cheap loop, and the two things I got wrong testing it

**What broke.** Nothing - a query whose filters match almost nothing returned an
almost-empty page and told the user to fix it themselves.

**What I tried.** The obvious loop re-runs `search()` after each relaxation,
which since the cross-encoder costs ~1.1s per attempt. Measured a capped count
over the same `_apply_filters()` instead: 11-24ms, where uncapped over 55,120
rows is 611ms - "how many" is a much harder question than "are there ten".

**What fixed it.** Walk the ladder on counts, then run exactly one real search. A
query needing no relaxation pays one extra count.

**Two things my own tests got wrong.** The first "starved" query I wrote was not
starved - `Cozy` AND `Horror` AND `Investigation` has been ANY-of since #33 - and
I briefly read that as a bug in the relaxation rather than in the test. And I
wrote the notes with em dashes, which a Windows console renders as a replacement
character; the CLI prints those strings too, so they are ASCII now. See
failures.md #39.

## 2026-09-08 - The hallucination checker was the thing hallucinating

**What broke.** The first full run of the grounded-explanation layer reported
**7.3%** discarded over 590 explanations - a publishable-looking number, and
wrong.

**What I tried.** Printed eight discards next to each game's real tags instead of
trusting the count. Three of the eight were the checker's fault: "but does not
include Fishing" flagged `Fishing` (the model DENYING a tag, which the prompt
asks it to do), "Experience the daily life of..." flagged `Experience` (a
sentence-initial verb that is also a tag), and "it is a Farming Sim" flagged
`Farming` (a short tag inside a long one).

**What fixed it.** Three guards on the prose scan: overlaps resolve
longest-first, a tag in a negated clause is a denial (scoped to its own clause,
so "not a puzzle game but it is Souls-like" still flags Souls-like), and a
sentence-initial single-word tag is grammar. Rate 7.3% -> **4.6%**, with prose
discards falling from 19 to 3.

16 of the original 43 "hallucinations" were mine, and every one of the three bugs
pushed the number UP - the direction that looks like diligence and therefore
never gets audited. The self-test is what made the rate worth doubting rather
than explaining away. See failures.md #38.

---

## 2026-09-07 - The reranker could not be served, twice, and torch lied about why

**What broke.** The plan said `ollama pull bge-reranker-v2-m3`. Ollama has no
rerank endpoint at all - `POST /api/rerank` is a 404, PR #7219 has been open
since 2024, and every community workaround scores through the *embedding*
endpoint, which is the bi-encoder the project already has.

**What I tried.** Hugging Face TEI as a compose service, which is the right shape
for this repo. It came up on **CPU**, with `Could not find a compatible CUDA
device` as a WARNING rather than an error, and sat in "Warming up model" for
eight minutes without going healthy. Chased the compose syntax first and was
wrong - a plain `docker run --gpus all` failed identically. The container's view
was the giveaway: `nvidia-smi` listed the 4080 by UUID while the CUDA driver API
answered `CUDA_ERROR_NO_DEVICE`, which is the signature of a user-mode/kernel-mode
driver mismatch (UMD 615.65.06 against KMD 616.56). Upgrading the host driver did
not move it: Docker Desktop ships driver libraries in its own managed WSL distro,
tracking Docker Desktop's version rather than the host's.

**What fixed it.** Ran the model in-process on the host, where CUDA has worked
all along. Then torch lied about the reason, which cost two more rounds: `uv add
torch` on Windows installs `2.14.0+cpu` from PyPI silently, and the only symptom
is `torch.cuda.is_available() == False`. Pointing uv at PyTorch's index was not
enough either - an unnamed `[[tool.uv.index]]` is just another index, and
resolution went back to PyPI. It needs a NAMED index with `explicit = true` plus
a `[tool.uv.sources]` binding.

**What to take from this.** Three layers each failed silently and in the
direction of "still works, just worse": Ollama 404s an endpoint that does not
exist, TEI warns and continues on CPU, torch installs a CPU wheel without
complaint. The 8-minute warm-up was the only reason any of it was noticed, and
what actually isolated the Docker fault was reproducing it OUTSIDE compose -
one command, and it should have been first rather than fifth.

## 2026-09-07 - Built the instrument that three changes had to go without

**What broke.** Nothing - the gap was in the measurement. Three parser changes in
a row (#33, #34, #35) could not be judged by `run_eval`, because not one of its
118 queries names a price, platform, year, age or game. The instrument always
votes for doing less.

**What I tried.** `eval/parse_cases.yaml` (47 labelled parses) and
`run_parse_eval.py`, scoring constraints MISSED and constraints INVENTED - the
second needs no labels, because any scalar a case does not name must come back
null. Then self-tested it by putting each of the day's bugs back by monkeypatch:
it caught three. The fourth, the invented all-three platforms, would not
reproduce with the guard off, so that arm is unproven rather than passing.

**What fixed it.** Baseline 319/319 fields, 0 flaky over 3 reps, 0 leaks, 3
known gaps still failing as documented. A regression guard rather than headroom.

## 2026-09-07 - A one-word title was unreachable, and the obvious fix was worse

**What broke.** Four of eight exclusion phrasings excluded nothing at all:
`without skyrim`, `except hades`, `not forza` found no reference. Not an
exclusion bug - `_word_ngrams` only makes 2-to-5 word windows, so a one-word name
can never be a candidate, and `like hades` borrowed no tags either.

**What I tried.** Measured the obvious fix before shipping it, and it was much
worse than the bug: generating every single word takes false positives over the
118 eval queries from 4 to 24 - "first person puzzle game" matched `Persona 5
Royal`, because `person` prefixes `Persona` - and each one appends six wrong tags
to `semantic_query`.

**What fixed it.** `_REFERENCE_CUE`: a single word is a candidate only when
`like` / `similar to` / `excluding` / `wie` / `ohne` precede it. Six of seven
titles found, false positives unchanged at 4/118. Cued singles are appended after
the longest-first windows so `call of duty` still beats `call`, and
`MIN_NAME_LENGTH` is 5 so Hades/Stray/Forza are reachable. `skyrim` is still
unreachable and should be - the real name is `The Elder Scrolls V: Skyrim`, which
is the prefix-match limitation from #21/#23. See failures.md #35.

## 2026-09-07 - A filter that also stayed in the query vector

**What broke.** A query asking for a popular game parsed to `min_reviews: 1000`
correctly - and left the word "popular" in `semantic_query`, so it was still
being embedded after becoming a SQL filter.

**What I tried.** Checked how general it was rather than fixing the one case: the
word survived into `semantic_query` on 8 of 9 queries that asked for it. Testing
the German side found a second bug - `_POPULAR` carried bare `beliebt` next to
`bekannt\w*`, so `beliebte Aufbauspiele`, the only form anyone writes, fired no
filter at all.

**What fixed it.** `_strip_popular()` beside `wants_popular()`, reusing
`_POPULAR` itself so trigger and removal cannot drift, called from the branch
that sets `min_reviews`. Guarded twice: skipped when stripping would leave
nothing, and run before `apply_reference` so the regex never sweeps the borrowed
tags. Plus `beliebt\w*`, with `beliebig` correctly still not matching.

Justified by correctness, not by a number: no eval query contains a popularity
word, so recall cannot see the change. The same leak is still open in
`wants_reference_excluded`. See failures.md #34.

## 2026-09-07 - The schema let the model skip fields, and the tags were ANDed

**What broke.** `...no wars on linux under 30$` extracted the platform and the
exclusion but never the price. The suspicion was the `$` sign.

**What I tried.** Killed that theory first - every notation parses alone and
every notation fails inside the full query. Then read the RAW model output
instead of the validated `ParsedQuery`: `max_price_usd` was **absent**, not null,
and `semantic_query` came FIRST. `_llm_schema()` had `required:
['semantic_query']`, because Pydantic only marks a field required when it has no
default, and Ollama's `format` turns an optional property into a grammar branch
the model can skip. `required_tags` was dropped on 64% of parses.

Forcing every field fixed extraction and made recall *worse* (46.5% against
54.1%), which exposed a second defect: `required_tags` was ANDed via `tags @>`.
Over 118 queries that helped 3 and hurt 11, and "running a bookshop and taking on
cosmic horror" returned zero rows - nothing carries `Cozy` AND `Horror` AND
`Investigation`. With `&&`, 8,544 games do.

**What fixed it.** `_REQUIRED_FIELDS` (the scalars plus `platforms` and
`semantic_query`; the tag arrays stay optional, since forcing those costs 15.9
points of tail) and `.overlap()` in `_apply_filters`. One GIN index serves both
operators, so no migration.

Requiring `platforms` then created a THIRD defect - on a query naming no OS the
model sometimes fills all three, and platforms are ANDed. Guarded in
`_drop_invented_platforms()`. My first check for invented constraints missed it
because I only counted the scalars and never looked at the one array in the
required set. Result: +4.2 overall (55.8% -> 60.0%), carried entirely by
`specific`. See failures.md #33.

## 2026-09-06 - "single player" parsed away, and the reference tags argued back

**What broke.** `call of duty like game, but not including itself, also popular,
single player` returned Counter-Strike at rank 1.

**What I tried.** Read the `filters:` line before touching the ranking, which is
what made this quick: `multiplayer` had come back null, so no `NOT EXISTS` clause
was ever built. Varied one clause at a time at temperature 0 - every shorter
phrasing gives `False`. Only all four clauses together, with the ask last, fails;
move "single player" earlier and it comes back. Isolating it also exposed an
unrelated defect: `apply_reference` had appended Call of Duty's `Multiplayer` tag
to the text being embedded, pushing the vector toward multiplayer exactly as the
user asked to play alone.

**What fixed it.** `wants_singleplayer()` beside `wants_popular()` - EN and DE,
negation-guarded, filling rather than overriding - plus `_contradicts_filters()`
so borrowed tags cannot fight `excluded_tags` or the multiplayer flag. The prompt
was not touched.

Nothing in the repo could have caught this: there was no singleplayer case in
`compare_parsers.py` or `queries.yaml`. And the before/after `--parse` runs
turned up something separate - exactly one query moved, one the change provably
cannot reach, so **the parser is deterministic within a run and not across
runs**. See failures.md #32.

## 2026-09-06 - The eval cannot tell 2.3 points from nothing

**What broke.** Arctic looked like it wanted `rrf w=0.10` rather than 0.20, on
the grounds that `tail` was 75.0% against 72.7%. A finer sweep disagreed with the
coarse one: `tail` came back 2.3 points lower at every weight, on the same model
and the same queries.

**What I tried.** Three experiments, cheapest first. The same config twice was
byte-identical, ruling out query-time nondeterminism. Rebuilding the HNSW index
over unchanged vectors was byte-identical, ruling out build order. That left the
vectors - and there was a specific cause: the first arctic corpus was embedded in
two halves under different `num_batch` settings, and batch size changes how the
forward pass is grouped, which changes float summation order.

**What fixed it.** Nothing in the code. The weight stays at 0.20, because the
entire case for 0.10 was 2.3 points and the reproducibility floor is 2.3 points.
Twenty minutes and four evals to decide NOT to make a change, which was the
cheapest outcome on offer. The guards do not help here either: a corpus embedded
two ways passes `verify_corpus_model()` and `verify_corpus_complete()` both.
Never change batch settings mid-corpus.

## 2026-09-06 - Arctic wins, and the reason qwen3 was picked was an artifact

**What broke.** A conclusion from two days earlier. failures.md #25 measured
three embedding models across `REVIEW_THRESHOLD` and found a crossover - qwen3
ahead below ~1,000 reviews - so qwen3 shipped, because threshold 10 is what
ships.

**What I tried.** Ran both models over the doubled 118-query set. Arctic wins by
7.5 points overall, 15.9 on `specific`, 9.1 on `tail`, with the counter-metric
unchanged, and it wins at `w=none` too - so it is neither buying recall by
deleting the tail nor interacting with the popularity term.

**What fixed it.** Realising what #25 actually measured: raising the threshold
DELETES rows from the corpus; it does not ask for an obscure game. Those are
different experiments and I had treated them as one. The `tail` tier asks
directly, and arctic wins there by 9.1. #25 was careful - it reported the whole
curve and reproduced every figure - and was still wrong about what the curve
meant. More conditions do not rescue the wrong measurement; only a different
measurement does.

Also worth keeping: measure the incumbent LAST and you pay for a third re-embed
to get back to the winner.

## 2026-09-06 - Doubled the eval, and it took back two of my claims

**What broke.** The arctic-vs-qwen3 result was split, and at n=22 per tier that
is two or three queries deciding an embedding model.

**What I tried.** Doubled both descriptive tiers to 44 and re-ran. Two things
fell out that I had not gone looking for: `specific` and `tail` were supposed to
be a controlled pair differing only in target popularity, but `tail` came from a
sampler while `specific` was hand-picked; and German recall jumped 31.0% ->
42.6% just from adding six queries, which is not how a language property
behaves.

**What fixed it.** `sample_specific.sql`, a mirror of the tail sampler differing
only in review band, and a tier-by-language matrix in `run_eval` - German is 37%
`core` against English's 22%, and per tier the gap was 4.2 / 30.2 / 12.5 against
an aggregate of 24.3. Same data, four answers. Growing a test set is not only a
power exercise: it re-runs every conclusion the old set produced.

## 2026-09-06 - A 400 from Ollama, with the reason thrown away

**What broke.** The arctic re-embed died at 21% on a bare
`httpx.HTTPStatusError: 400 Bad Request`. No reason in the traceback, because
`raise_for_status()` discards the response body.

**What I tried.** Hunted for a poisoned row first, which was wrong twice over:
the longest `embed_text` is 1,091 bytes, so no row can exceed ~1,100 tokens, and
re-running the exact failing page passed. Only then read Ollama's server log,
which had said it all along: `input (3002 tokens) is too large to process
(current batch size: 2048)`. Ollama packs several inputs into one server task and
checks the PACKED count; the tasks either side were 114 and 134 tokens.

**What fixed it.** Three things, in the order they matter. `_reason()` pulls
Ollama's own message into the exception - the fix for the hour rather than for
the bug. `num_batch` 4096 raises the ceiling to the model's context. And
`_post_batch` halves a rejected batch and retries with a WARNING per split.

**Then it fired in production on a cause I had not predicted:** at 98% of the
resumed run, Windows ephemeral-port exhaustion inside Ollama's own tokenize call
after ~130k requests in 17 minutes. `num_batch` would not have touched it; the
general-purpose net caught it and the run finished clean. When a specific fix and
a broad one are both cheap, the broad one is what pays.

## 2026-09-06 - The long tail tier works, and half of the fix was theatre

**What broke.** Nothing - the new tier does what it was built for: `tail` is flat
from w=none through 0.20, falls at 0.40, and collapses to 9.1% at w=2.00 while
`core` climbs to 35.6%. What broke is my account of WHY the previous attempt
failed.

**What I tried.** I had fixed two things: the sampler's `ORDER BY total_reviews
DESC`, and queries written while reading `short_description`. Instead of trusting
the second, I measured it: content-word overlap with the target's own text is 37%
in both tiers, and 9 of 22 new targets sit at pure-cosine rank 1 against the old
tier's 7 of 22. By my own stated mechanism the new tier is MORE contaminated. It
works anyway.

**What fixed it.** The sampler, alone. RRF pays a famous rank-1 target on both
terms while an obscure one sits at the bottom of the popularity rank, so target
obscurity prices the weight and query prose does not. I shipped two changes, one
mechanical and one that merely sounded disciplined, and measured them separately
almost by accident. Measure the parts of a fix apart, or you learn the wrong rule
from a real win.

## 2026-09-06 - The long-tail eval tier was the 97th percentile

**What broke.** I wrote 22 long-tail queries and swept the weight. Recall on the
new tier ROSE with the weight, when the entire point of the tier was that it
should fall.

**What I tried.** Checked the targets rather than the ranker, on the principle
that a metric behaving backwards is usually the metric. Percentile of each of the
22 app_ids: median **97.1**, none below 93.4. One clause in my own sampler -
`DISTINCT ON (tags[1]) ORDER BY total_reviews DESC` - keeps the MOST-reviewed
game per tag, so a 50-5,000 band returned its top edge. A second bias underneath:
I wrote each query while reading the game's `short_description`, which is inside
`embed_text`, so targets sat at cosine rank ~1, where the popularity term
arithmetically cannot move them.

**What fixed it.** Not more labels - a metric that uses none. `run_eval` prints
the median review count of everything returned and the share under 1,000. It
priced the weight immediately: median returned goes 65 -> 165 -> 4,021 at
w = none -> 0.2 -> 1.0, and 0.20 is the knee of the curve. The tier was renamed
`specific`. A labelled tier is only as unbiased as its SAMPLING, and target-first
query writing fixes bias in choosing queries, not in choosing targets. See
failures.md #27.

---

## 2026-09-05 - The eval wanted a popularity weight that deletes the long tail

**What broke.** The sweep asked for far too much popularity: recall@10 climbed
18.3% -> 35.6% as the weight rose, peaking at `rrf w=2.0`. Taking that number
would have been the whole point of the exercise, and wrong.

**What I tried.** Two controls, because a metric that only goes up is not
measuring what you think. Ranking by popularity ALONE scores 32.2% against the
best blend's 35.6% - so of a 17.3-point gain, 13.9 came from sorting by review
count. And the ground truth explains why: all 37 expected app_ids have >= 11,267
reviews, so recall rises with the weight until the corpus is gone.

| weight (log) | recall@10 | median reviews returned | under 1k |
| --- | --- | --- | --- |
| 0.00 | 18.3% | 68 | 79% |
| 0.05 | 25.0% | 192 | 69% |
| 0.20 | 31.1% | 4,715 | 30% |
| 1.00 | 35.6% | 17,889 | **1%** |

**What fixed it.** Choosing the weight by the defect it repairs rather than the
metric it moves: `rrf w=0.20`, the smallest setting that puts Cities: Skylines II
above a 27-review asset flip for "city builder" while leaving 70% of results in
the tail. `rrf` over `log` because at matched tail cost they are equivalent, so
the tiebreak is durability - log's weight is calibrated against a model's cosine
spread, rrf reads only ranks.

**Latency, separately.** `run_eval`'s median times the whole `search()` call,
embedding included, and Ollama's embed time swung 94-834ms after a host restart,
making ranking look 20x slower than it is. Time the thing you changed, not the
pipeline containing it.

## 2026-09-04 - The 1024-dim HNSW index is 2x the size and quietly costs recall

**What broke.** Two predictions about migration 0007 were wrong at once. The plan
estimated ~680MB (510MB scaled by 1024/768); it came out at **1020MB**. And
re-running the eval with the index in place scored LOWER than the exact scan -
15.0% against 18.3%.

**What I tried.** For the size, divided it by the page size instead of guessing:
exactly 1.000 pages per element. A 1024-dim float32 vector is 4,096 bytes, and
with the neighbour list an element lands near 4.4KB - so two cannot share an 8KB
page and each wastes ~45%. A packing cliff at 4KB, not a 33% dimension increase.
For the recall, `hnsw.ef_search` was never set, so it ran at pgvector's default
of 40.

**What fixed it.** `hnsw.ef_search = 200` restored exact-scan recall at every
threshold, for 45ms -> 53ms against 225ms for the exact scan. Applied in
`app/search.py` via `SET LOCAL`, not in the database - a GUC pinned on the
database is exactly the invisible drift the Alembic rule exists to prevent.
(Since raised to 800; see 2026-09-19.)

Worth carrying: `halfvec` at 1024 dims is 2,048 bytes and would roughly halve the
index. Untested here.

## 2026-09-04 - No leaderboard picked the winner, and the winner depends on the threshold

**What broke.** The first model comparison was run piecemeal, and bge-m3's
pre-eval check used `min(embedding_model)`, which returns `bge-m3` whether or not
arctic vectors are still in the same column. The numbers might have been measured
over a mixed corpus, with no way to tell afterwards.

**What I tried.** Re-ran all three end to end with the same verification before
every eval: `count(DISTINCT embedding_model)` (not `min`), distinct
`vector_dims`, pending count, and a degenerate-vector check via
`(embedding <#> embedding) = 0`.

**What fixed it.** Nothing needed fixing - all thirty numbers reproduced exactly,
which demonstrated rather than assumed that the `embedding IS NULL` work queue
makes a resumed job write the same vectors.

| threshold | corpus | arctic | bge-m3 | qwen3:0.6b |
| --- | --- | --- | --- | --- |
| 10 | 55,120 | 8.3% | 10.0% | **18.3%** |
| 1,000 | 7,212 | 26.1% | 22.8% | **26.7%** |
| 10,000 | 1,702 | **57.2%** | 49.4% | 38.3% |

**The ranking inverts between 1,000 and 10,000**, so "which model is best" has no
answer without naming the threshold. No leaderboard predicted it: bge-m3 leads
MIRACL by 13 points and finished last or joint-last at four of five thresholds.
(That crossover reading was itself withdrawn two days later - see above.)

## 2026-09-04 - The embedding model was not the problem; ranking was

**What broke.** Baseline recall@10 was 6.1% overall (9.2% EN, 0.0% DE) on
`nomic-embed-text`. Re-dimensioning to 1024 and re-embedding with arctic gave
8.3% overall - DE unstuck, but **EN went down**. A better model moved almost
nothing.

**What I tried.** Reading the actual results instead of the score. For "city
builder", every game whose NAME contains the query outranked Cities: Skylines II
and its 73,524 reviews. Cosine similarity has no notion of prominence, and in a
corpus that is mostly shovelware a nameless asset-flip called literally "City
Builder" wins the lexical match every time.

**What fixed it.** Nothing yet - the finding is that ranking needs a popularity
TERM, not a cliff. Sweeping `REVIEW_THRESHOLD` gives 7x from a config value
(8.3% at 10 to 57.2% at 10,000), and the peak is real rather than circular: it
FALLS at 50,000, where the filter starts eating expected games. Threshold 10,000
is not adopted, because it costs 128,949 of 130,651 games and the long tail is
the product.

## 2026-08-29 - A search took 18 seconds, and none of it was searching

**What broke.** A filtered CLI search reported `embed 18017ms | query 417ms`.
Embedding one short string took eighteen seconds.

**What I tried.** `ollama ps` showed the model resident with `UNTIL: 4 minutes
from now`. Ollama's default `keep_alive` is 5 minutes, after which it evicts the
model, so an idle CLI pays a cold load to do ~20ms of arithmetic.

**What fixed it.** Pass `keep_alive` in the `/api/embed` body, from a new
`OLLAMA_KEEP_ALIVE` setting defaulting to `30m`. The model is 323MB against 16GB
of VRAM, so holding it is free.

Separately, that run measured `query 417ms` against the 19.6ms I estimated while
planning - the estimate used an existing row's embedding as the probe, whose
neighbours already matched the filters. Benchmark with real queries.

## 2026-08-26 - HNSW index build failed with "No space left on device"

**What broke.** Migration 0003 died with `could not resize shared memory segment
to 2144407040 bytes`. The host had 588GB free.

**What I tried.** Read the byte count: 2,144,407,040 is exactly the 2GB set as
`maintenance_work_mem`. Not disk at all - Docker gives a container 64MB of
`/dev/shm`, Postgres coordinates parallel workers through shared memory, and it
surfaces as a disk error because /dev/shm is a filesystem.

**What fixed it.** `shm_size: 4gb` on the db service, then recreate the
container. It is a ceiling rather than a reservation. Serialising the build also
avoids it, but leaves the same trap for the next parallel operation. DDL is
transactional, so the failed CREATE INDEX rolled back and alembic_version stayed
at 0002.

## 2026-08-26 - Every price in the database was a sale price

**What broke.** Spot-checking the fresh ingest against Steam, Stardew Valley read
$8.99 where its real price is $14.99. Not a parsing error - the column held
exactly what the source said.

**What I tried.** Checked the source record and found a `discount` field the
loader had dropped. So `price` is the price on the day of the scrape, and the
scrape caught a sale: 41,712 of 110,709 paid games (37.7%) were discounted, and
filtering "under $20" wrongly admitted 3,004 games.

**What fixed it.** Migration 0002 adds `discount_pct` plus a generated
`list_price_usd` reversing the discount, guarded at both ends (100% would divide
by zero, and 6 games are at 100%). The loader coerces `discount`, which the
source stores as str for 102,759 records and int for 36,205.

Two things worth remembering. The derived list price is a cent low, because Steam
rounds sale prices down - fine for filtering, don't display it as exact. And when
verifying, Python and Postgres disagreed on one row: Python's float gave
20.000000000000004 and excluded it, Postgres' numeric gave exactly 20.00.
Postgres was right. That is what `numeric(10,2)` is for.

## 2026-08-20 - Embeddings ran at 0.5/sec on a 4080 SUPER

**What broke.** First smoke test of Ollama embeddings took 2.1s per call - 71
hours for 120k games.

**What I tried.** Checked `ollama ps` first, assuming CPU fallback: it said
`100% GPU`, so the time was per-request overhead. Benchmarked four combinations:
`localhost` vs `127.0.0.1`, fresh connection per call vs one reused
`httpx.Client`.

**What fixed it.** `localhost` on Windows resolves to IPv6 `::1` first, and
Ollama listens on IPv4 only, so every new connection stalls ~2.1s before falling
back. Using the IP directly: 0.5 -> 17.6/sec. Reusing one client: 35.7/sec.
Batching via `/api/embed` with a list input: 62.3/sec. 125x total, no hardware
change.

Consequences: `.env.example` uses `127.0.0.1` for both Ollama and Postgres -
psycopg would hit the identical stall - and the embedding client must hold one
long-lived `httpx.Client` and send batches.
