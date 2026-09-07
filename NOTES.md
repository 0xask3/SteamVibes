# Notes

What broke, what I tried, what fixed it. Newest first.

---

## 2026-09-07 — The reranker could not be served, twice, and torch lied about why

**What broke.** BUILD_PLAN's Weekend 4 item 1 says `ollama pull
bge-reranker-v2-m3`. Ollama has no rerank endpoint at all — `POST /api/rerank`
is a **404** on 0.33.3, PR #7219 has been open since 2024, and every community
workaround scores through the *embedding* endpoint, which is the bi-encoder the
project already has. So the one-line instruction was never going to run.

**What I tried.** Hugging Face TEI as a compose service, which is the right
shape for this repo — a container with a real `/rerank` route, direct HTTP, same
as Ollama. It came up on **CPU**, with `Could not find a compatible CUDA device`
as a WARNING rather than an error, and then sat in "Warming up model" for eight
minutes without ever going healthy.

Chased the compose syntax first and was wrong: switched
`deploy.resources.reservations.devices` to the modern `gpus: all`, and a plain
`docker run --gpus all` on the same image failed identically. Not the compose
file.

The container's view was genuinely strange, and each of these is a thing that
usually IS the answer:

```
nvidia-smi -L        GPU 0: NVIDIA GeForce RTX 4080 SUPER (UUID: GPU-e219df2a...)
/dev/dxg             present
libdxcore.so         present
/usr/lib/wsl/drivers populated
CUDA driver API      DriverError(CUDA_ERROR_NO_DEVICE)
```

NVML works and the CUDA driver API does not, which is the signature of a
**user-mode/kernel-mode driver mismatch**. The container was being handed UMD
615.65.06 against KMD 616.56. Upgrading the host driver to 616.56 and restarting
WSL did not move it: the mounted `libcuda.so.1` stayed byte-identical (188,024
bytes, dated Aug 20), because Docker Desktop ships driver libraries in its own
managed WSL distro and those track **Docker Desktop's** version, not the host's
NVIDIA driver. `NVIDIA_DISABLE_REQUIRE=1` did not help either. This is the
documented Docker Desktop WSL2 / NVIDIA Container Toolkit incompatibility;
NVIDIA's own guidance is to use Docker CE inside WSL2 instead.

**What fixed it.** Ran the model in-process on the host, where CUDA has worked
the whole time — Ollama has been using that GPU all session. BUILD_PLAN sanctions
it in the same line as the Ollama suggestion: "or run it via
sentence-transformers".

Then torch lied about the reason, which cost two more rounds and is the part
worth remembering. `uv add torch` on Windows installs **`2.14.0+cpu` from PyPI**,
silently, and the only symptom is `torch.cuda.is_available() == False` — which
reads exactly like a broken GPU rather than a wrong wheel. Pointing uv at
PyTorch's index was not enough either: an unnamed `[[tool.uv.index]]` is still
just another index, and resolution went back to PyPI. It needs a NAMED index with
`explicit = true` plus a `[tool.uv.sources]` binding, or torch quietly stays on
CPU:

```toml
[[tool.uv.index]]
name = "pytorch-cu130"
url = "https://download.pytorch.org/whl/cu130"
explicit = true

[tool.uv.sources]
torch = { index = "pytorch-cu130" }
```

`2.14.0+cu130`, `cuda available: True`, 15.8GB free on the 4080. Reranking 200
candidates takes **253ms warm**, against the 8 minutes TEI spent not finishing a
warm-up on CPU.

**Consequences.** The TEI compose service is deleted rather than left in place —
a service that cannot work is a trap for whoever reads the file next. Two real
costs stay: the containerised backend cannot rerank, because torch is not in that
image and the GPU is not either (the same honest limitation ingest already has),
and `RERANK_DEVICE=cuda` is now checked loudly at load, because
sentence-transformers falls back to CPU without saying so and a CPU eval is not a
slower measurement, it is an unusable one.

**What to take from this.** Three separate layers each failed silently and in the
direction of "still works, just worse": Ollama 404s an endpoint that does not
exist, TEI warns and continues on CPU, and torch installs a CPU wheel without
complaint. None of them raised. The 8-minute warm-up was the only reason any of
it got noticed at all — and the thing that actually isolated the Docker fault was
reproducing it OUTSIDE compose, which took one command and should have been the
first thing I did rather than the fifth.

---

## 2026-09-07 — Built the instrument that three changes had to go without

**What broke.** Nothing, this time - the gap was in the measurement. Three parser
changes in a row (#33, #34, #35) could not be judged by `run_eval`, because not
one of its 118 queries names a price, a platform, a year, an age or a game. Every
filter the parser extracts can only shrink the candidate set, so recall punishes
extraction and can never reward it. The instrument always votes for doing less.

**What I tried.** `eval/parse_cases.yaml` (47 labelled parses) and
`eval/run_parse_eval.py`. It scores the two things recall cannot see: constraints
MISSED, and constraints INVENTED - the second needs no labels, because any scalar
field a case does not name is required to come back null. It also checks that a
constraint which became a filter left `semantic_query` (the #34 rule), defaults
to 3 reps with a `flaky` column (the #33 rule), and keeps `known_gap` cases in
the file but out of the score.

Then self-tested it, because a harness that passes everything on the first run
might be measuring nothing: put each of today's bugs back by monkeypatch and
check it goes red. It caught three - the all-optional schema (4 missed), the
popularity leak (6 of 7 leaked), the unreachable one-word titles (6 missed, 3/4
exclusions). The fourth, the invented all-three platforms, would not reproduce at
all with the guard off, so that arm is unproven rather than passing. Recorded as
a correction on #33.

**What fixed it.** Baseline: 319/319 fields, 0 flaky over 3 reps, 4/4 exclusions,
0 leaks, 3 known gaps still failing as documented. That is a regression guard
rather than headroom - the parser passes everything currently labelled, which is
the expected state after fixing four bugs in it today.

---

## 2026-09-07 — A one-word title was unreachable, and the obvious fix was worse

**What broke.** Went to fix the exclusion-phrase leak that #34 called "arguably
worse" than the popularity one. It is not — the excluder word survives in 2 of 8
phrasings, not 8 of 9, and what stays behind is a real game name, which is useful
signal rather than noise. But four of those eight queries excluded nothing at
all: `without skyrim`, `except hades`, `not forza` all found no reference.

**What I tried.** Split reference-matching from excluder-matching, which showed
the excluder logic was fine and `_find_referenced_game` was the problem.
`_word_ngrams` only makes 2-to-5 word windows, so a one-word name can never be a
candidate — `Hades` has 279,741 reviews and nothing prefixes it. `MIN_NAME_LENGTH
= 6` blocked it again at five characters. Not an exclusion bug at all: `like
hades` borrowed no tags either, which is the whole point of `title_lookup`.

Then measured the obvious fix before shipping it, and it was much worse than the
bug: generating every single word takes false positives over the 118 eval queries
from 4 to 24 — "first person puzzle game" matched `Persona 5 Royal`, because
`person` prefixes `Persona` — and each false positive appends six wrong tags to
`semantic_query`.

**What fixed it.** `_REFERENCE_CUE`: a single word is only a candidate when
`like` / `similar to` / `excluding` / `wie` / `ohne` and friends precede it. Six
of seven titles found, false positives unchanged at 4/118 with the same four
identities. `_word_ngrams` untouched, cued singles appended after the
longest-first windows so `call of duty` still beats `call`, and `MIN_NAME_LENGTH`
5 so Hades/Stray/Forza are reachable.

Consequences: `skyrim` is still unreachable and should be — the real name is `The
Elder Scrolls V: Skyrim` and no query prefix opens it, which is the prefix-match
limitation from #21/#23. And the eval cannot score any of this; the 118 queries
serve as a false-positive corpus, not a recall measure. See failures.md #35.

---

## 2026-09-07 — A filter that also stayed in the query vector

**What broke.** Asked why "a game where we play as a cat exploring city or ruins,
which is also popular" did not return Stray. Stray was not the bug — it sits at
cosine rank 20 and RRF at `w=0.20` cannot lift that past rank-1 matches — but the
parse showed something else: `min_reviews: 1000` correctly extracted, and
`semantic_query: 'cat exploring city or ruins popular'`. The word was still being
embedded after it had already become a SQL filter.

**What I tried.** Checked how general it was rather than fixing the one case: the
popularity word survived into `semantic_query` on 8 of 9 queries that asked for
it. Then tested the German side and found a second bug — `_POPULAR` carried bare
`beliebt` next to `bekannt\w*`, so `beliebte Aufbauspiele`, the only form anyone
actually writes, fired no filter at all.

**What fixed it.** `_strip_popular()` next to `wants_popular()`, reusing
`_POPULAR` itself so the trigger and the removal cannot drift apart, called from
the branch that sets `min_reviews`. Guarded twice: skipped when stripping would
leave nothing (`parse_query`'s empty check runs before `_apply_code_rules`), and
run before `apply_reference` so the regex never sweeps the borrowed tag list.
Plus `beliebt\w*`; `beliebig` correctly still does not match.

Consequences: this is justified by correctness, not by a number. Stray moves from
rank 20 to 17 and is still not in the top 10, and no eval query contains a
popularity word so recall cannot see the change at all. The same leak is still
open in `wants_reference_excluded` — "excluding call of duty" embeds the excluded
franchise's own name, pulling the vector toward exactly what SQL is removing. See
failures.md #34.

---

## 2026-09-07 — The schema let the model skip fields, and the tags were ANDed

**What broke.** `game that feels like call of duty, but no wars on linux under
30$` extracted the platform and the exclusion but never the price. Suspicion was
the `$` sign instead of the word "dollars".

**What I tried.** Killed the `$` theory first: every notation parses correctly
alone (`30$`, `$30`, `30 dollars`, `30 USD`, `30 bucks`) and every notation fails
inside the full query, so it is neither the symbol nor the wording. Then read the
RAW model output instead of the validated `ParsedQuery`, which is what actually
showed it — `max_price_usd` was **absent**, not null, and `semantic_query` was
emitted FIRST. `_llm_schema()` had `required: ['semantic_query']`, because
Pydantic only marks a field required when it has no default. Ollama's `format`
turns an optional property into a grammar branch the model can skip, so any
filter could silently vanish, and absence reads downstream as "not requested".
Measured the blast radius: `required_tags` dropped on 64% of parses.

Forcing every field fixed extraction and made recall *worse* (46.5% against
54.1%), which turned out to be a second, hidden defect: `required_tags` was ANDed
via `tags @>`. Over 118 queries it helped 3 and hurt 11, six queries
under-delivered, and "running a bookshop and taking on cosmic horror" returned
zero rows — nothing carries `Cozy` AND `Horror` AND `Investigation`. With `&&`,
8,544 games do.

**What fixed it.** `_REQUIRED_FIELDS` in `query_parser.py` (the scalars plus
`platforms` and `semantic_query`; the tag arrays stay optional, since forcing
those costs 15.9 points of tail) and `Game.tags.contains()` → `.overlap()` in
`_apply_filters`. One GIN index serves both operators, so no migration.

Requiring `platforms` then created a third defect — on a query naming no OS the
model sometimes fills all three, and platforms are ANDed, so it silently demands
Windows AND macOS AND Linux. Guarded in `_drop_invented_platforms()`. My first
check for invented constraints missed it because I only counted the scalar
fields and never looked at the one array in the required set.

Consequences: parse goes 0.56s → 1.07s, purely output tokens, so the API's
first-search path goes ~1.35s → ~1.85s (chip edits still do not re-parse).
Result is +4.2 overall against a same-session baseline (55.8% → 60.0%), carried
entirely by `specific` (+11.4); `tail` is down 2.3, at the floor. And the eval
could not have refereed this on its own — only 7 of 118 queries carry any
constraint and none names a price, platform, year or age, so `--parse` scores how
little the parser does rather than how well it parses.

The `--parse` reproducibility floor is also wider than #32 said: four queries
moved between two runs of identical code, which is 3.4 points. Two wrong
diagnoses came out of chasing them before I re-ran and watched them move on their
own. See failures.md #33.

---

## 2026-09-06 — "single player" parsed away, and the reference tags argued back

**What broke.** `call of duty like game, but not including itself, also popular,
single player` returned Counter-Strike at rank 1. Counter-Strike is not single
player.

**What I tried.** Read the `filters:` line before touching the ranking, which is
what made this quick: it printed `>= 1,000 reviews  not Call of Duty®` and
nothing else, so `multiplayer` had come back null and no `NOT EXISTS` clause was
ever built. Varied one clause at a time at temperature 0 — every shorter
phrasing gives `False`, including `call of duty like game, single player` and
`... also popular, single player`. Only all four clauses together, with the ask
last, fails; move "single player" earlier in the same sentence and it comes
back. That is failures.md #13/#22 again: the prompt is full and the last thing
mentioned is what falls off.

Isolating it with a hand-built `ParsedQuery` also exposed a second, unrelated
defect: `apply_reference` had appended Call of Duty's tags — including
`Multiplayer` — to the text being embedded. So the vector was being pushed
toward multiplayer at the moment the user asked to play alone. Same shape for
"like resident evil but nothing scary", which excluded `Horror` in SQL while
embedding it.

**What fixed it.** `wants_singleplayer()` in `query_parser.py` beside
`wants_popular()` — EN and DE, negation-guarded, and it *fills* rather than
overrides — plus `_contradicts_filters()` in `title_lookup.py` so borrowed tags
cannot fight `excluded_tags` or the multiplayer flag. The prompt was not
touched. Reported query now returns Ravenfield, Call of Juarez: Gunslinger and
SUPERHOT, with zero of the top ten carrying a multiplayer category.

Consequences: nothing in the repo could have caught this — there was no
singleplayer case in `compare_parsers.py` *or* `queries.yaml`. Both new cases
are now in `compare_parsers.py`. And the before/after `run_eval --parse` turned
up something separate: exactly one query moved, one my change provably cannot
reach, which means **the parser is deterministic within a run but not across
runs** at temperature 0. `--parse` has a reproducibility floor of its own, at
least one query wide. See failures.md #32.

---

## 2026-09-06 — The eval cannot tell 2.3 points from nothing

**What broke.** Arctic looked like it needed `rrf w=0.10` rather than the shipped
0.20, on the grounds that `tail` was 75.0% at 0.10 and 72.7% at 0.20. Before
proposing a ranking change I swept finer — 0.10 through 0.25 — to find the actual
cliff. The finer sweep disagreed with the coarse one: `tail` came back 2.3 points
lower at every weight, `core` and `specific` identical. One query out of 44, on
the same model and the same queries.

**What I tried.** Three experiments, cheapest first. The same config run twice
was byte identical, ruling out query-time nondeterminism. Dropping and rebuilding
the HNSW index over unchanged vectors was byte identical, ruling out graph build
order. That left the vectors — and there was a specific cause, not general GPU
noise: the first arctic corpus was embedded in two halves under different
`num_batch` settings, 2,048 for the first 27,648 rows and 4,096 for the rest,
because I added that setting midway through fixing the crash. Batch size changes
how the forward pass is grouped, which changes float summation order, which flips
whatever sits near a tie.

**What fixed it.** Nothing needed fixing in the code. The weight stays at 0.20,
because the entire case for 0.10 was 2.3 points and the reproducibility floor is
2.3 points. The model verdict survives — arctic wins by 6.7 overall and 15.9 on
`specific`, eight and seven queries, comfortably clear of a one-query floor.

Twenty minutes and four evals to decide *not* to make a change, which was the
cheapest outcome on offer: the alternative was a ranking change justified by a
number I had never checked was real. The uncomfortable part is point 3 in
failures.md #31 — having measured the floor, several differences I reported
earlier this week sit under it. And the guards do not help here: a corpus
embedded two different ways passes `verify_corpus_model()`, which sees one model
name, and `verify_corpus_complete()`, which sees no gaps. Never change batch
settings mid-corpus.

## 2026-09-06 — Arctic wins, and the reason qwen3 was picked was an artifact

**What broke.** Nothing today — what broke was a conclusion from two days ago.
failures.md #25 measured three embedding models across `REVIEW_THRESHOLD` and
found a crossover: qwen3 ahead below ~1,000 reviews, arctic ahead above it by 19
points. qwen3 shipped because threshold 10 is what ships. That reasoning was
wrong, and it took building a different instrument to see it.

**What I tried.** Ran both models over the doubled 118-query set, same grid, same
index settings. Arctic wins by 7.5 points overall, 15.9 on `specific`, 9.1 on
`tail`, with the counter-metric unchanged — so it is not buying recall by
deleting the long tail. It wins at `w=none` too, so it is not an interaction with
the popularity term either.

**What fixed it.** Realising what #25 actually measured. Raising
`REVIEW_THRESHOLD` deletes rows from the corpus; it does not ask for an obscure
game. Those are different experiments and I had treated them as one. The `tail`
tier asks directly — 44 queries whose right answer has 30-300 reviews — and
arctic wins there by 9.1 points. There was never a regime where qwen3 found
obscure games better; there was a regime where the corpus had been cut to 1,702
rows and both models were scored on 30 queries about famous ones.

#25 was careful. It reported the whole curve instead of one number, reproduced
every figure in a second pass after finding its own verification unsound, and
was still wrong about what the curve meant. More conditions do not rescue the
wrong measurement — only a different measurement does. Every correction this
week has had that shape: #26 the ground truth, #27 the fix for the ground truth,
#29 the language split, and now #25's threshold curve. The ranker has been fine
throughout. What kept being broken was what I was holding up to it.

One more thing worth keeping, because it cost 26 minutes: the corpus is on qwen3
right now, so shipping arctic needs a third re-embed. Ordering matters when each
measurement costs half an hour — measure the incumbent last and you have to pay
again to get back to the winner.

## 2026-09-06 — Doubled the eval, and it took back two of my claims

**What broke.** The arctic-vs-qwen3 result was split — arctic +13.7 on
`specific`, −7.2 on `core`, level everywhere else — and at n=22 per tier that is
two or three queries deciding an embedding model. Not a result, an anecdote.

**What I tried.** Doubled both descriptive tiers to 44 and re-ran: 118 queries,
German 27. Two things fell out that I had not gone looking for. `specific` and
`tail` were supposed to be a controlled pair differing only in target
popularity, which is the whole basis of failures.md #28 — but `tail` came from a
sampler and `specific` was hand-picked, so sampling method varied too. And
German recall jumped 31.0% → 42.6% just from adding six queries, which is not how
a language property behaves.

**What fixed it.** `sample_specific.sql`, a mirror of the tail sampler differing
only in the review band, so the pair is now controlled in fact rather than in
the write-up. And a tier-by-language matrix in `run_eval`, because the aggregate
EN/DE rows were measuring query mix as much as language: German is 37% `core`
queries against English's 22%, and per tier the gap is 4.2 / 30.2 / 12.5 points
against an aggregate of 24.3. Same data, four answers.

The part worth keeping is that the expansion was not meant to audit anything. It
was meant to add statistical power for a model decision, and on the way it
falsified one claim I had written as settled and corrected another I had stated
too confidently. Meanwhile #28's actual finding — `tail` flat then collapsing
while `specific` climbs — reproduced exactly on 22 queries written afterwards
against a different model, which is far better evidence than the original run.
Growing a test set is not only a power exercise; it re-runs every conclusion the
old set produced.

## 2026-09-06 — A 400 from Ollama, with the reason thrown away

**What broke.** The arctic re-embed died at 21% (27,648 of 130,651) on a bare
`httpx.HTTPStatusError: 400 Bad Request` from `/api/embed`. No reason in the
traceback, because `raise_for_status()` discards the response body.

**What I tried.** Hunted for a poisoned row first, which was wrong twice over.
`max(length(embed_text))` is 694 characters and `max(octet_length())` is 1,091
bytes, so with byte fallback no row can exceed ~1,100 tokens. Re-ran the exact
uncommitted page — the commit is per 1,024 rows, so the failing page was still
`NULL` and perfectly reproducible — and all eight 128-row chunks passed. Only
then read `%LOCALAPPDATA%\Ollama\server.log`, which had said it all along:
`input (3002 tokens) is too large to process. increase the physical batch size
(current batch size: 2048)`. Ollama packs several inputs into one server task
and checks the PACKED count against `n_ubatch`; the tasks either side of the
rejected one were 114 and 134 tokens.

**What fixed it.** Three things, in the order they matter. `_reason()` now pulls
Ollama's own message into the exception, which is the fix for the hour rather
than for the bug. `options.num_batch` (new setting, 4096) raises the ceiling to
the model's context — verified in the log as `n_ubatch = 4096`. And `_post_batch`
halves a rejected batch and retries, with a WARNING per split, so a 30-minute
job survives a transient instead of dying at 21%.

The retry logic was covered by a stub rather than the real server, because the
packing anomaly cannot be summoned on demand: 32 inputs at a deliberately low
ceiling each got their own task and sailed through. What the stub verified was
the recursion, the ordering and the terminal raise, not the condition that
triggers them.

**Then it fired in production, on a cause I had not predicted.** At 98% of the
resumed run (101,376 of 103,003):

    Ollama rejected a batch of 128 (Post "http://127.0.0.1:53372/tokenize":
    dial tcp: bind: An operation on a socket could not be performed because the
    system lacked sufficient buffer space or because a queue was full.).
    Retrying as 64 + 64.

That is Windows ephemeral-port exhaustion inside Ollama's own internal tokenize
call after ~130k requests in 17 minutes — nothing to do with batch sizes, and
`num_batch` would not have touched it. The run finished clean: 130,651 vectors,
0 pending, 0 zero-vectors.

Which is the useful lesson. The targeted fix addressed the cause I had
diagnosed; the general-purpose net caught a different one nobody had thought of,
17 minutes into a job that would otherwise have died at 98%. When a specific fix
and a broad one are both cheap, the broad one is what pays.

The lesson is the boring one. The explanation was one layer away in a log file
the whole time, and I spent the detour bisecting a corpus that could not
physically contain the reported input. An exception that swallows the body turns
a one-line diagnosis into an hour of guessing.

## 2026-09-06 — The long tail tier works, and half of the fix was theatre

**What broke.** Nothing, this time — which is why it is worth writing down. The
tier built this morning does what it was built for: `tail` recall is flat at
63.6% from w=none through w=0.20, falls to 59.1% at 0.40, and collapses to 9.1%
at w=2.00 while `core` climbs to 35.6%. First honest peak-and-fall in this
project. What broke is my account of *why* the previous attempt failed.

**What I tried.** The entry above blamed two things and I fixed both: the
sampler's `ORDER BY total_reviews DESC`, and queries written while reading
`short_description`, which is inside `embed_text`. The second fix was to write
"in the words a player would use." Instead of trusting that, I measured it
against the old tier: content-word overlap with the target's own text is 37% in
both, and 9 of 22 new targets sit at pure-cosine rank 1 against the old tier's
7 of 22. By my own stated mechanism the new tier is *more* contaminated. It
works anyway.

**What fixed it.** The sampler, alone. Cosine rank was never the whole story:
RRF scores `1/(k+r_cos) + w/(k+r_pop)`, so a rank-1 target that is also famous
gains on both terms, while a rank-1 target with 60 reviews sits at the bottom of
`r_pop` and the same weight pushes it down. Target obscurity prices the weight;
query prose does not. `rrf w=0.20` holds — now because it is the last setting
that costs the tail nothing, not because it is a corner on a proxy curve.

The lesson is about the shape of the fix rather than the ranker. I shipped two
changes, one mechanical and one that merely sounded disciplined, and measured
them separately almost by accident. Together they would have been recorded as a
success and the useless half repeated on the next tier. Measure the parts of a
fix apart, or you learn the wrong rule from a real win.

## 2026-09-06 — The long-tail eval tier was the 97th percentile

**What broke.** NOTES 2026-09-05 ended needing labelled queries with obscure
answers, so recall could see what a popularity weight deletes. I wrote 22 and
swept the weight. Recall on the new tier rose with the weight — 54.5% at w=0.2
to 68.2% at w=1.0 — when the entire point of the tier was that it should fall.

**What I tried.** Checked the targets rather than the ranker, on the principle
that a metric behaving backwards is usually the metric. Percentile of each of
the 22 app_ids against the corpus: median **97.1**, none below 93.4. The cause
was one clause in my own sampler — `DISTINCT ON (tags[1]) ORDER BY tags[1],
total_reviews DESC` keeps the *most*-reviewed game per tag, so a 50-5,000 band
returned its top edge. Then I picked the ones I recognised. A second bias
underneath it: I wrote each query while reading the game's `short_description`,
which is inside `embed_text`, so the targets sat at cosine rank ~1 — and RRF's
popularity term is capped at `w/(k+1)`, worth about fifteen rank places at
w=0.2. It arithmetically cannot move a rank-1 hit, so the tier could not have
reported harm regardless of the weight.

**What fixed it.** Not more labels — a metric that uses none. `run_eval` now
prints the median review count of everything returned and the share under 1,000
reviews. No ground truth, no query authorship, so neither bias can reach it. It
priced the weight immediately: median returned goes 65 → 165 → 4,021 reviews at
w = none → 0.2 → 1.0, and under-1k share 79% → 70% → 23%. Core recall bought per
point of tail surrendered is 0.74 at w=0.2 and 0.17 at w=0.4, so **0.20 is the
knee of the curve**, not just the cautious pick it was shipped as. The tier is
renamed `specific` — it does measure something real, whether a detailed
description finds its one game, just not what it was named for. See
failures.md #27.

Consequence: a labelled tier is only as unbiased as its *sampling*, and
target-first query writing does nothing about that — it fixes bias in choosing
queries, not in choosing targets. Where a counter-metric can be computed without
labels, prefer it; it cannot be talked into agreeing with you.

---

## 2026-09-05 — The eval wanted a popularity weight that deletes the long tail

**What broke.** Adding a prominence term to ranking worked, and the sweep then
asked for far too much of it. recall@10 at threshold 10 climbed 18.3% -> 25.0
-> 28.3 -> 31.1 -> 34.4 -> 35.6% as the weight rose, peaking at `rrf w=2.0`
(equivalently `log w=1.0`). Taking that number would have been the whole point
of the exercise, and wrong.

**What I tried.** Two controls, because a metric that only goes up is not
measuring what you think.

First, rank by popularity *alone* - semantic similarity still selects the
200-candidate pool but contributes nothing to the ordering. That scores
**32.2%**, against 35.6% for the best blend and 18.3% for pure similarity. So of
a 17.3-point gain, 13.9 came from sorting by review count and 3.4 from the
embedding.

Second, look at the ground truth. All 37 expected app_ids have **>= 11,267
reviews**, median 84,488, none under 1,000. The labelled set contains no
long-tail games at all, so recall@10 rises with the popularity weight until the
corpus is gone. The metric cannot see the cost, so measure the cost directly -
what the 30 eval queries actually return:

| weight (log) | recall@10 | median reviews returned | results under 1k |
| --- | --- | --- | --- |
| 0.00 | 18.3% | 68 | 79% |
| 0.05 | 25.0% | 192 | 69% |
| 0.10 | 28.3% | 674 | 54% |
| 0.20 | 31.1% | 4,715 | 30% |
| 0.40 | 34.4% | 12,366 | 6% |
| 1.00 | 35.6% | 17,889 | **1%** |

The eval-optimal weight returns almost nothing under 1,000 reviews. That is
`REVIEW_THRESHOLD=10000` by another route - the exact trade refused a day
earlier, arrived at from the other direction and with a better-looking number
attached.

**What fixed it.** Choosing the weight by the defect it repairs rather than by
the metric it moves: `rrf w=0.20`, the smallest setting that puts Cities:
Skylines II above a 27-review asset flip for "city builder" while leaving 70% of
results in the tail. Costs 10 points of recall against the eval optimum and
keeps the product.

`rrf` over `log` because at matched tail cost they are equivalent - log 0.05 and
rrf 0.20 both give 25.0% at ~70% tail; log 0.20 and rrf 1.00 both give 31.1% -
so the tiebreak is durability. log's weight is calibrated against the model's
cosine spread (qwen3's top 10 spans 0.752-0.696, arctic's 0.577-0.502); rrf
reads only ranks and survives a model swap unchanged, which matters while the
model choice is still provisional.

Final: 25.0 / 26.7 / 30.0 / 41.1 / 42.2% across the five thresholds, against
18.3 / 22.8 / 26.7 / 38.3 / 38.9 before.

**The real conclusion is that the eval needs long-tail ground truth.** Until it
has some, no larger weight can be justified from it, and the honest reading of
35.6% is "this metric rewards popularity", not "ranking improved by 17 points".

**Latency, separately.** `run_eval`'s "median search latency" times the whole
`search()` call, embedding included, and Ollama's embed time swung between 94ms
and 834ms after a host restart - which made the ranking look 20x slower than it
is. Measured apart, query time is 44-85ms at every threshold and an alternating
A/B put rrf at ~57-60ms against ~40ms unranked. Time the thing you changed, not
the pipeline containing it.

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
