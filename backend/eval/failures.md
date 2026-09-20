# Queries that fail

Weekend 1 output: type in vibes, write down what does not work. These became the
eval set (`eval/queries.yaml`), so each entry records what was asked, what came
back, and why. Later entries are deeper: a mechanism, the measurement that
settled it, and any claim it withdrew.

Baseline for #1-#11: `nomic-embed-text`, pure vector similarity,
`total_reviews > 10`.

---

## 1. Negation is ignored entirely

**Query.** `game that's similar to call of duty, but we shooting plants, that's
not plants vs zombies`

**Got.** Positions 1, 2 and 3 were Plants vs. Zombies titles.

**Why.** The query is embedded as a single vector. The string "plants vs
zombies" is present, so the vector lands near it and "not" barely moves it.
Bi-encoder embeddings have no mechanism for exclusion: the retrieval is correct,
the request is not expressible.

**Fixed by** the query parser, which lifts negations into `excluded_tags` before
the remainder is embedded. Title exclusion came later, in #21/#23.

---

## 2. Proper nouns as similarity anchors

**Query.** Same - "similar to call of duty".

**Why.** "Call of Duty" as a token carries little semantic weight against words
like "military" or "shooter". Vector similarity cannot reliably use a named
title as a reference point.

**Fixed by** `app/title_lookup.py` (#21), not by hybrid retrieval: the game's own
row already carries the tags the query is reaching for.

---

## 3. Hard constraints are treated as vibes

**Query.** `co-op game under 20 dollars that runs on linux`

**Got.** Windows-only games. Price and platform ignored.

**Why.** "linux" and "under 20 dollars" are embedded as TEXT, so the query
matches games whose descriptions read like cheap Linux co-op games. Similarity
has no access to `games.linux` or `games.list_price_usd`, though both are
populated, correct and indexed. This is the clearest argument for the hybrid
design: the right answer is one WHERE clause away and pure semantic search
cannot reach it.

**Fixed by** the parser plus SQL filters.

---

## 4. Titles outweigh tags

**Query.** `very easy relaxing game with no challenge`

**Got.** Two of the top ten are tagged `Difficult`: *EasyPianoGame* and *Ease
Out*. *No Time to Relax* (#2) is a party game about stress.

**Why.** `embed_text` leads with the name, which is short, so it carries
disproportionate weight against fifteen tags. A game called *EasyPianoGame* reads
as being about ease.

**Later disproved as a fixable cause - see #19.** The antonym test this came from
otherwise passed: "brutally difficult" and "very easy relaxing" returned disjoint
lists, so polarity IS carried by the tags.

---

## 5. Self-description beats reputation

**Query.** `brutally difficult game that will make me suffer`

**Got.** Almost entirely long-tail games - 21, 31, 28, 18, 14 reviews. No Dark
Souls, Getting Over It, Celeste or Super Meat Boy.

**Why.** Obscure games literally write "This game is hard" in their description.
Famous difficult games describe world and tone; their difficulty is reputation,
carried in reviews and culture rather than in the text we embed. The loudest
self-describers win.

---

## 6. Player context read as game content

**Query.** `something I can play one-handed while eating`

**Got.** Games *about* hands and food: Don't Cut Your Hand, Hungry Tiger, Eat
Your Words.

**Why.** The query describes the player's situation, and every word becomes a
topic to match against descriptions. Nothing distinguishes "a game I play while
eating" from "a game about eating".

**Hard to fix.** "one-handed" is not in Steam's tag vocabulary at all. Some
intents are not expressible in this data.

---

## 7. Gaming jargon read literally

**Query.** `game where the map changes every run`

**Got.** Worm Runner, Speedrun, and Fantasy Map Simulator - a map *creation*
tool. No roguelikes at all.

**Why.** "Run" means a playthrough in games and locomotion in general English;
"map changes" was read as editing maps. The query describes procedural
generation without using the term. Related: `time loop where you replay the same
day` drifted to rewind-mechanic games.

**Fixed by** tag grounding - the parser sees the real vocabulary, so this becomes
`required_tags=["Rogue-lite"]`. The strongest argument for grounding on all 452
real tags rather than letting the model invent.

---

## 8. Number tokens in titles, and an unsafe result

**Query.** `game for a 7 year old that isn't violent`

**Got.** #1 Paintball 707 (a shooter). #5 Knockout Daddy, tagged `Violent`. #9
Chill, tagged `Nudity`. #2 and #4 matched on the digit 7.

**Why.** Negation ignored (#1) plus title-token matching (#4). The digit 7 is a
strong literal signal with no semantic connection to a child's age.

**Schema gap this exposed.** The source JSON carries `required_age` and the 0001
schema omitted it. Added in 0004 - but only 1,321 games have a non-zero value, so
age filtering must also exclude mature tags. Recommending a game tagged `Nudity`
to someone shopping for a seven-year-old is exactly the plausible-looking wrong
answer this project is about.

---

## 9. Social constraints ignored

**Query.** `something to play with my mum who has never gamed before`

**Got.** Mostly singleplayer educational games aimed at children.

**Why.** Two failures at once. "Play *with*" implies co-op, and the categories
that say so went unused. And "never gamed before" was conflated with "for
children"; a beginner adult is not a child.

**Partly fixed.** `multiplayer=true` maps onto `game_categories`. The
beginner-vs-child conflation is a semantic limit, not a missing filter.

---

## Works well (calibration)

`game with a grappling hook that feels great to swing` - all ten results are
genuine grappling-hook games, top score 0.802, the highest seen. Specific
mechanics that appear verbatim in descriptions work well. Caveats even here: #7
*Hook* matched on title, and no canonical examples (Just Cause, Spider-Man)
appear - the same reputation-versus-self-description problem as #5.

## Resolution of #5: crowded out, not unreachable

At `--threshold 5000` the same query returns Getting Over It at #1, plus Darkest
Dungeon and Thymesia. The canonical hard games are reachable; 130k long-tail
entries were simply ranked in front of them. Still absent even there: Dark
Souls, Elden Ring, Sekiro, Celeste. Their descriptions sell setting and tone.

## 10. "Brutal" reads as gore, not as challenge

At threshold 5000, half the list matched violence rather than difficulty: Blood
Trail, Bloody Hell ("Bloody" for "brutally"), Dark Deception. "Brutally" and
"suffer" sit closer to violence and horror in embedding space than to mechanical
difficulty. Maps cleanly onto the real tag `Difficult` - another vote for
grounding the parser in the vocabulary.

## 11. Selective filters cost 10x latency

Query time went from ~150ms at `--threshold 10` to **1446ms** at 5000, same
query, because `hnsw.iterative_scan` re-scans until LIMIT is satisfied. That is
the price of the correctness fix: without it the query silently returns four
results instead of ten.

**Correction to an earlier measurement.** The 2.8ms figure recorded when choosing
iterative_scan used an existing row's embedding as the query vector - a point
already in the index with dense neighbours. A real query embedding lands in
sparser space and costs far more. Benchmark with real queries, not with rows
already indexed: a stacked-filter search measured 417ms against a 19.6ms probe.

---

## Resolved by structured filters (2026-08-29)

`app/search.py` takes a `ParsedQuery` and applies price, platform, tag, year, age
and multiplayer filters in SQL before pgvector ranks the survivors.

- **#3 fixed.** Every result Linux, under $20, genuinely co-op.
- **#8, the tools exist.** `--max-age 7` plus tag exclusions express it;
  `required_age` alone is insufficient, so the tag exclusions do most of the work.
- **#9 partly.** `--multiplayer` maps onto `game_categories`, using the full
  co-op set because 744 of 22,127 co-op games lack `Multi-player` itself.
- **#7 partly.** Turning the phrase into the tag is the parser's job.
- **Still open:** #1 negation in free text, #2 proper nouns, #4, #5, #6, #10.

## Query parser results (2026-08-29)

`qwen3.5:9b` against `qwen3.5:4b` over ten queries, `format` +
`temperature=0`. Round 1 produced **10 disagreements out of 10** - at temperature
0 that is a prompt problem, not model variance.

### 12. Schema field order is reasoning order

`semantic_query` came back as the entire original sentence, unstripped, on 4 of
10 queries. `format` constrains generation, so fields are emitted in declaration
order and an earlier one cannot be revised - and `semantic_query` was declared
FIRST, so the model had to write the constraint-stripped query before extracting
any constraints. Fixed by moving it last; it is the only field whose value
depends on all the others.

### 13. Prompt layout is load-bearing, and the two blocks compete

With the ~1,400-token vocabulary at the BOTTOM, tags extracted well but
price/platform/year were ignored. Moving it to the TOP fixed the scalars and
collapsed the tags - "cozy farming game with fishing" went from three tags to
zero. Whichever block sits nearest the query wins.

Fixed by vocabulary at the bottom PLUS rewriting the scalar rules to hold on
wording rather than position, PLUS removing a blanket "no filter is better than a
wrong one" line that was suppressing tags. Any future edit here needs
`compare_parsers.py` re-run, not just a read.

### 14. Mood read as player count

`multiplayer=false` invented on 6 of 20 parses - "cozy farming game",
"entspanntes Spiel zum Abschalten". A wrong `multiplayer` is worse than a missing
one: it silently removes every co-op game. Fixed by a prompt rule naming "cozy",
"relaxing", "entspannt" and "gemütlich" as saying nothing about player count. 6
occurrences to 0.

### 15. Vague price words invented a number

"cheap relaxing puzzle games" produced `max_price_usd=20` on 9b and `0` on 4b -
the `0` being the dangerous one, since it restricts to free games silently. Fixed:
a price requires a NUMBER or the word free/kostenlos.

### 16. `released_after` missed by 9b - OPEN

`free multiplayer shooter released after 2020`: 9b returns the price and
multiplayer but no year. 4b gets it reproducibly. Cause unknown; the rule is
stated plainly and the field sits sixth of nine.

### 17. Co-op collapses into `multiplayer`, losing the tag - OPEN

`required_tags` comes back empty on the prompt's own worked example, probably a
refusal to double-encode: once "co-op" became `multiplayer=true`, the `Co-op` tag
reads as redundant. Defensible, and pgvector recovers it - but `Base-Building` is
an available hard filter going unused.

### Model comparison

**Chosen: `qwen3.5:9b`.** 4b returns no filters at all on two of ten queries, one
of them German; 9b's failures are single omissions. The 0.14s difference sits in
front of a ~417ms search. Selection was on evidence, not size - 4b beat 9b on #16
and on `Souls-like`, and was a live contender until the two `(none)` rows decided
it.

### 18. `Remote Play Together` counted as multiplayer

A Single-player village builder ranked 8th for "co-op base builder". Its full
category list is `Single-player, Remote Play Together, ...` - and
`MULTIPLAYER_CATEGORIES` included Remote Play Together, which is a STREAMING
feature: it sends one player's screen to a friend, so a single-player game
qualifies.

    matched by the filter      23,750
    genuinely multiplayer      23,113
    false positives (RPT only)     637

Fixed by dropping that one entry. **Worth noting how it was found.**
`compare_parsers.py` could never have caught it - the parser was entirely
correct here and the filter underneath it was wrong. Checking parsed filters and
checking returned games are two different tests.

---

## Measured non-fixes (2026-09-04)

Two proposed improvements, both tested before implementation, both wrong. A
disproved idea is worth as much as a confirmed one and costs more to re-derive
than to read.

### 19. `embed_text` recipe: no measurable effect - NOT WORTH DOING

**Hypothesis.** A short name first outweighs the tags (#4), so name-last or
repeated tags would fix it - "one f-string plus ~12 min re-embedding".

**Test.** Real `embed_text` for 14 games, three recipes crossed with the nomic
prefixes, scored against "extremely hard to beat game like elden ring".

**Result.** All six variants: **1 of 5 relevant in the top 5**, rankings
unmoved. Removing the name entirely left *Elden: Path of the Forgotten*, *Elo
Hell* and *Elude* above *Hollow Knight*.

**Why.** Deleting `{name}` from the f-string does not remove the name from the
embedded text: **32,201 of 130,633 descriptions (25%) begin with the game's own
name** and 38% contain it somewhere. The f-string never controlled the thing it
was blamed for.

**Also tested: nomic task prefixes.** Adding them nudged real souls-likes up
~0.03 and changed no ranking. Principled, not worth a re-embed on its own.

### 20. Embedding-based tag shortlisting - WOULD REGRESS

**Hypothesis.** The prompt carries all 452 tags and is demonstrably saturated
(#13), so embed the query and keep only the ~30 nearest tags.

**Test.** Embedded all 452 tags and checked where the tags currently extracted
land:

| query | tag needed | rank of 452 |
|---|---|---|
| extremely hard to beat game like elden ring | `Souls-like` | **133** |
| cheap relaxing puzzle games, nothing scary | `Horror` | **171** |
| gemütliches Aufbauspiel für zwei | `Cozy` | **354** |
| cozy farming game with fishing | `Fishing` | 0 |

**Result.** A top-40 shortlist would delete tags that work today, in three
classes: negation inverts the vector ("nothing scary" embeds nowhere near
`Horror`), German collapses, and franchise references do not work - which is the
whole point of the query. It works only for direct English topical mentions,
which are the cases already succeeding. Abandoned.

**What it did reveal.** "elden ring" cannot embed near `Souls-like` - but the
ELDEN RING row carries that tag already. That is #21.

### 21. Franchise references, fixed by looking the game up

**Before.** `extremely hard to beat game like elden ring, not including itself`
returned ELDEN RING at #1, then *Elden: Path of the Forgotten*, *Elo Hell*,
*Elude*, *Elems* - games matching the string "Eld", not the meaning. And "not
including itself" was silently dropped: `ParsedQuery` could exclude tags, never
titles.

**Fixed by** `app/title_lookup.py`: if a game name appears in the query and
clears `TITLE_MATCH_MIN_REVIEWS`, its top 6 tags are appended to
`semantic_query` and its app_id goes into `excluded_app_ids` when asked. No LLM,
one SQL statement, 1-2ms.

**After.** ELDEN RING NIGHTREIGN, *The Memory of Eldurim*, **DARK SOULS™ II**,
*Trapped Souls*, *Alaloth*. Every string-match result gone but one.

**The review floor is the whole trick.** Common English words are real Steam
titles: `Nothing` (9,260 reviews), `Something`, `SELF`, `Dollar`, `Beat`, `GAME`.
Without a floor, "nothing scary" matches a horror game called *Nothing*. At
50,000, seven test queries produced zero false positives while still finding
ELDEN RING and Stardew Valley.

**Tags are appended, not required.** Requiring all six of a game's tags returns
almost nothing; biasing the query vector has no such cliff.

**Still open (and the proposed remedy was wrong - see #23).** Franchise names
with trademark or edition suffixes do not match, because the stored name is
LONGER than the query text.

### 22. "popular" had nowhere to go - and the prompt is full, confirmed

**Query.** `I like FPS shooters, suggest some excluding call of duty, which are
also popular`. Results between 43 and 6,007 reviews. "which are also popular" was
stripped from `semantic_query` and then discarded, because `ParsedQuery` had no
popularity concept at all.

**Fixed by** `min_reviews`, raising search's floor with `max(base, min_reviews)`
so it can never drop below the baseline quality gate.

**The prompt attempt, and its cost.** The two earlier regressions (#13) came from
editing the TAG block, so the hypothesis was that the SCALAR block would be safe.
It was not - one added rule, measured over 12 queries:

| query | before | after the rule |
|---|---|---|
| ...feels like call of duty ... on linux | `linux  not War` | `linux` |
| rundenbasierte Strategie mit Koop-Modus | `Turn-Based Strategy  Co-op  multiplayer` | `Turn-Based Strategy  Co-op` |

Reverted, and detected in code instead as a regex. **This is the third
confirmation that the prompt is saturated**, and the first that position within
it does not matter. The price is that a stated number - "at least 500 reviews" -
is not understood. One unhandled phrasing is cheaper than a filter that silently
stops working.

### 23. Franchise names - prefix matching, no pg_trgm needed

**Corrects #21**, which recorded this as needing trigram matching, an extension
and a migration. It needed none of those: the diagnosis was right and the
proposed remedy was wrong.

**The fix.** Invert the comparison. Take word windows from the query and ask
whether any of them OPENS a name: `lower(name) LIKE :phrase || '%'`.

| query | matched | before |
|---|---|---|
| ...like call of duty... | **Call of Duty®** (714,114) | *(none)* |
| something like dark souls... | **DARK SOULS® III** (413,775) | *(none)* |
| nothing scary / 20 dollars / 7 year old | *(none)* | *(none)* |

1-26ms, and the same review floor keeps false positives at zero.

**Exclusion had to be generalised twice.** `wants_reference_excluded` matched
only "not including *itself*", so it also looks for an excluder within two words
of the matched phrase. Then excluding one app_id proved insufficient - *Modern
Warfare* and *Black Ops Cold War* stayed - so the exclusion is a prefix too, 24
franchise entries rather than one.

**Result** for the #22 query: before, Aim Hero / Aimtastic / FPSBois (48
reviews); after, Squad (210k), Arma 3 (283k), Battlefield 2042, Insurgency.
Three mechanisms had to work together - the review floor removed shovelware, the
borrowed tags pulled the vector toward military shooters, and the franchise
exclusion removed what was asked for. None alone was enough.

**Known behaviour change.** Prefix exclusion catches sequels and spinoffs.
Defensible, but a judgement call rather than a derivation.

### 24. The corpus outranks itself: no quality term in ranking (2026-09-04)

Baseline was 6.1% recall@10 (9.2% EN, 0.0% DE) on `nomic-embed-text`, and DE at
zero made it look like a German problem. It was not - the German queries are
near-parallel, so DE's ceiling IS EN's 9.2%.

Replacing the model barely moved it: arctic gave 8.3% overall, DE unstuck but
**EN down**. The score hid what the results made obvious. Top 10 for `city
builder`:

    1. City Builder 0.577 ... 6. Square City Builder 0.509
    8. Cities: Skylines II (73,524 reviews) 0.502

Every game whose NAME restates the query beats the canonical answer. Nothing is
wrong with the retrieval - those are all genuinely city builders. Cosine has no
notion of prominence, and in a corpus that is overwhelmingly shovelware an asset
flip named literally "City Builder" wins the lexical match every time.

Sweeping `REVIEW_THRESHOLD` confirms it - 8.3% at 10, 26.1% at 1,000, **57.2%**
at 10,000, falling to 47.2% at 50,000. 7x from a config value. The peak is not an
artifact of famous ground truth: that would climb monotonically, and it FALLS
where the filter starts eating expected results.

**Not adopting 10,000.** It buys 57% by deleting 128,949 of 130,651 games. The
long tail is the product. The finding is that ranking needs a CONTINUOUS quality
term, not a cliff the user has to ask for - and that is a ranking change, so it
goes through plan mode.

### 25. Three models, and the ranking inverts at the threshold (2026-09-04)

recall@10, exact scan (HNSW dropped, so ground truth rather than index recall),
30 labelled queries:

| threshold | corpus | arctic | bge-m3 | qwen3:0.6b |
| --- | --- | --- | --- | --- |
| 10 | 55,120 | 8.3% | 10.0% | **18.3%** |
| 1,000 | 7,212 | 26.1% | 22.8% | **26.7%** |
| 10,000 | 1,702 | **57.2%** | 49.4% | 38.3% |
| 50,000 | 470 | **47.2%** | 44.4% | 38.9% |

**The models swap places** between 1,000 and 10,000. A single-number comparison
at one threshold would have picked either model with no warning that the other
choice existed. Report the curve.

**Leaderboards did not predict any of it.** bge-m3 leads MIRACL by 13 points and
came last or joint-last at four of five thresholds; arctic leads MTEB Retrieval
and loses at the threshold in use.

**Method note, from getting it wrong the first time.** Pass 1 was run piecemeal
and bge-m3's pre-eval check used `min(embedding_model)`, which cannot detect the
one failure it exists to prevent. Pass 2 re-ran all three from `--reload` with
`count(DISTINCT embedding_model)` plus a zero-vector check. All thirty numbers
reproduced. The verification was genuinely unsound; the results were not. A
check that cannot fail is not a check.

**Shipped qwen3** because threshold 10 is what ships - **and #30 withdraws this
entry's crossover reading entirely.**

### 26. The ground truth has no long tail, so recall bought one (2026-09-05)

Built the popularity term #24 asked for, with two blend methods behind a config
flag so the eval could choose. It chose badly, and how it failed is more useful
than the feature: recall@10 nearly doubled as the weight rose, peaking at `rrf
w=2.0`.

**Control 1: rank by popularity ALONE.** 32.2%, against 35.6% for the best blend
and 18.3% for pure similarity - so 13.9 of the 17.3 points came from sorting by
review count and 3.4 from the embedding. Most of the "ranking improvement" is not
ranking.

**Control 2: look at the labels.** All 37 expected app_ids have >= 11,267
reviews, **none under 1,000**. The eval contains no long-tail games, so recall
can only rise as the weight rises. It is structurally incapable of reporting the
cost.

**The cost, measured directly:**

| weight (log) | recall@10 | median reviews | under 1k |
| --- | --- | --- | --- |
| 0.00 | 18.3% | 68 | 79% |
| 0.20 | 31.1% | 4,715 | 30% |
| 1.00 | 35.6% | 17,889 | **1%** |

At the eval optimum, 1% of results have under 1,000 reviews - the same trade #24
refused, reached from the other side and wearing a better number.

**Shipped `rrf w=0.20`**: the smallest weight that fixes the observed defect
while leaving 70% of results in the tail, ten points below the eval optimum,
deliberately. `rrf` over `log` because they are equivalent at matched tail cost,
so the tiebreak is durability - log's weight is calibrated against a model's
cosine spread, rrf reads only ranks.

**What to do about it.** `queries.yaml` needs labelled queries whose answers are
obscure. Until then the harness cannot tell a better ranker from a more popular
one. Fourth falsified hypothesis, and the first where the METRIC rather than the
idea was wrong.

### 27. The fix for #26 was labelled long-tail queries. They were not long tail (2026-09-06)

I wrote 22 obscure-answer queries and got the opposite of the predicted shape:
recall on the new tier ROSE with the weight. Two mistakes, both mine, both in the
ground truth rather than the ranker.

**Mistake 1 - the sampler returned the head of the tail.** `DISTINCT ON
(g.tags[1]) ... ORDER BY total_reviews DESC` keeps the MOST-reviewed game per
tag, so a 50-5,000 band returned its top edge; I then picked the 22 I recognised.
Every target sits in the 93rd-98th percentile, median **97.1**, against a median
searchable game of ~90 reviews.

**Mistake 2 - the queries were paraphrases of the embedded text.** I wrote each
query while reading the `short_description` that is part of `embed_text`. That
puts the target at cosine rank ~1, and RRF's popularity term is bounded by
`w/(k+1)` - about fifteen rank places at w=0.2, so it CANNOT dislodge a rank-1
hit. The tier reported "no harm" by construction.

**The measurement that does work, and needs no labels.** Median review count of
everything returned, and the share under 1,000 - it cannot be gamed by target
selection or query authorship, because it uses neither. It priced the weight
immediately: median returned goes 65 -> 165 -> 4,021 at w = none -> 0.2 -> 1.0.

**`rrf w=0.20` stands, on a different argument than #26 gave it.** Core recall
gained per point of under-1k share surrendered: **0.74** for none -> 0.20, then
0.17 and 0.12 above it. 0.20 is a corner, not a preference.

Fifth falsified hypothesis, and the second running where the metric was wrong:
#26 caught the ground truth being biased, #27 is the fix for that bias having the
same bias.

### 28. A long-tail tier that works, and the half of the fix that did nothing (2026-09-06)

22 new queries, targets sampled at 30-300 reviews - the 37th-75th percentile
against `specific`'s 97th:

| w | core (30) | specific (22) | **tail (22)** | median revs | under 1k |
| --- | --- | --- | --- | --- | --- |
| none | 18.3% | 54.5% | **63.6%** | 65 | 81% |
| 0.20 | 25.0% | 54.5% | **63.6%** | 138 | 73% |
| 0.40 | 26.7% | 59.1% | **59.1%** | 395 | 63% |
| 1.00 | 31.1% | 68.2% | **36.4%** | 3,306 | 28% |
| 2.00 | 35.6% | 68.2% | **9.1%** | 8,219 | 8% |

Flat, then a peak, then collapse - the shape #26 asked for. `specific` rises
across the same sweep that takes `tail` from 63.6 to 9.1, with the same query
style, the same author and the same afternoon: the only variable that differs is
target popularity.

**The weight, on direct evidence at last.** w=0.20 is the largest weight that
costs the tail literally nothing while core gains 6.7 points. Same number, third
and best reason.

**The half that did nothing.** #27 blamed two things and fixed both. Measured
afterwards rather than assumed: content-word overlap with the target's own
`embed_text` is **37% in both tiers**, and 9 of 22 tail targets sit at cosine
rank 1 against `specific`'s 7 of 22. By #27's own stated mechanism the new tier
is MORE contaminated. It works anyway.

The explanation was incomplete: RRF pays a famous rank-1 target on both terms
while an obscure one sits at the bottom of the popularity rank, so the identical
term pushes it down. **Target obscurity prices the weight. Query prose does
not** - the discipline lives in the sampler, not in the writing, which is the
opposite of where the effort went. Shipped together and called a success, the
prose rule would have been recorded as the thing that worked.

**Not fixed, deliberately.** Four tail targets are outside the top 200 under pure
cosine and score zero at every weight. Dropping them would raise the headline and
would be exactly the target-selection bias this entry is about.

### 29. The eval doubled, and two of #28's claims did not survive it (2026-09-06)

At n=22 per tier one query is 4.5 points, so an entire model verdict rested on
two or three queries. The eval grew to **118 - core 30, specific 44, tail 44,
German 27.**

**The good news: #28's shape reproduced out of sample**, on queries written
afterwards against a different embedding model - flat, then falling, while
`specific` climbs monotonically.

**Claim that was overstated: "the only variable is target popularity."** `tail`
came from a sampler and `specific` was hand-picked, so sampling method varied
too. `sample_specific.sql` now mirrors the tail sampler exactly, only the band
differing, and its targets land at the 97.3rd percentile against the hand-picked
97.1. The claim is now true rather than merely plausible; it was written as
though it already were, which is the actual mistake.

**Claim that was wrong: the EN/DE split measures language.** German recall moved
31.0% -> 42.6% purely from adding six queries. The sets do not have the same tier
mix - German is 37% `core` against English's 22%:

| tier | EN | DE | gap |
| --- | --- | --- | --- |
| core | 19.2% (20) | 15.0% (10) | 4.2 |
| specific | 85.7% (35) | 55.6% (9) | 30.2 |
| tail | 75.0% (36) | 62.5% (8) | 12.5 |
| aggregate | 66.8% (91) | 42.6% (27) | 24.3 |

Same data, four different answers. `run_eval` prints this matrix now, with the
aggregate labelled as not a language measurement.

**Expanding the eval invalidated the qwen3 baseline**, because recall is not
comparable across query sets. That cost was accepted deliberately: the
alternative was picking a model on three queries.

### 30. Arctic wins, and #25's crossover story was an artifact of its instrument (2026-09-06)

| @ rrf w=0.20 | qwen3 | arctic | delta |
| --- | --- | --- | --- |
| core (30) | **25.0%** | 17.8% | -7.2 |
| specific (44) | 63.6% | **79.5%** | **+15.9** |
| tail (44) | 63.6% | **70.5%** | **+6.9** |
| overall | 53.8% | **60.5%** | **+6.7** |
| median revs / under 1k | 132 / 74% | 147 / 74% | - |

One query is 0.85 points at n=118, so +6.7 overall is about nine queries. It
holds at `w=none` too, so it is retrieval quality rather than an interaction with
the popularity term, and the counter-metric is unchanged. **Ship arctic.**

**#25's crossover was an artifact.** Raising `REVIEW_THRESHOLD` DELETES rows from
the corpus, which is not the same experiment as ASKING for an obscure game. The
`tail` tier asks directly, and arctic wins it by 6.9 points. There was never a
regime where qwen3 found obscure games better; there was a regime where the
corpus had been cut to 1,702 rows and both models were scored on 30 queries about
famous ones. #25 was careful, reported the whole curve, reproduced every figure -
and was still wrong about what the curve meant. Running more conditions does not
help when all of them are the wrong measurement.

**Arctic's gain is English-only.** `specific` English goes 65.7 -> 85.7 while
German sits at 55.6 for both models. Swapping to the model whose selling point is
multilingual retrieval bought 20 points of English and nothing German, which
points at `embed_text` rather than the encoder.

**qwen3 wins `core` alone**, the tier documented as understating quality: arctic
returns Cities: Skylines II and Roguebook, both correct, both unlisted, both
scoring zero. It also returns a 44-review "Megacity Builder", which is a real
defect. Both are true and `core` cannot separate them.

*(The arctic column originally read tail 72.7 / overall 61.3, measured on a
corpus embedded in two halves under different `num_batch` settings - see #31.)*

### 31. The eval has a reproducibility floor, and it is one query wide (2026-09-06)

A finer weight sweep disagreed with the coarse one: same model, same 118 queries,
same config, `tail` 2.3 points lower at every weight while `core` and `specific`
came back identical. One query out of 44.

**Isolating it, cheapest experiment first:**

| test | result | rules out |
| --- | --- | --- |
| same config twice | byte identical | query-time nondeterminism |
| drop and rebuild HNSW, same vectors | byte identical | index graph build order |
| re-embed the same model | `tail` moves 2.3 points | leaves the vectors |

> **2026-09-19:** the middle row does not reproduce. Five parallel builds of the
> same vectors gave 66.8-68.8% at `ef_search=200`, and a re-embed at a fixed
> `num_batch` is bit-identical - so today the index moves and the vectors do not.
> This entry's own cause, the mixed-`num_batch` corpus, still stands. See #42.

The first arctic corpus was embedded in two halves under **different `num_batch`
settings** (27,648 rows at 2,048, then 103,003 at 4,096). Physical batch size
changes how the forward pass is grouped, which changes floating-point summation
order, which flips results near a tie.

**Consequences.**

1. **The weight stays at 0.20.** The whole case for 0.10 was 2.3 points, and the
   floor is 2.3 points. Adopting it would have been fitting to noise with a
   ranking change to show for it.
2. **The model verdict is unaffected** - arctic's margin is eight and seven
   queries, comfortably above a one-query floor.
3. **Differences under ~2.5 points at n=44, or ~1 point at n=118, are not
   results.** Three of that week's entries reported differences in that range.
4. **Never change embedding batch settings mid-corpus.** Nothing downstream can
   detect it: `verify_corpus_model()` sees one model name and
   `verify_corpus_complete()` sees no gaps.

Four extra evals and an index rebuild, about twenty minutes, to decide NOT to
make a change - the cheapest outcome available.

---

### 32. "single player" was parsed away, and the reference tags argued with the filters (2026-09-06)

**Reported.** `call of duty like game, but not including itself, also popular,
single player` returned **Counter-Strike** at rank 1. Two independent defects,
both in the parser path, neither in the ranking - which is where I would have
looked first if the `filters:` line had not printed the answer.

**Defect 1: the filter was never applied.** The model returned
`multiplayer=None`, so no `NOT EXISTS` clause was built. Not a general failure,
and deterministic at temperature 0:

| query | `multiplayer` |
| --- | --- |
| `call of duty like game, single player` | `False` |
| `... also popular, single player` | `False` |
| **`... but not including itself, also popular, single player`** | **`None`** |
| `... single player, but not including itself, also popular` | `False` |

Every clause individually is fine; all four together with the ask LAST is not,
and moving it earlier in the same sentence brings the filter back. #13 and #22
for the third and fourth time: the prompt is full, and what falls off is whatever
the query mentions last.

**Defect 2: `apply_reference` borrowed tags that contradict the filters.**

| query | the WHERE clause | the text being embedded |
| --- | --- | --- |
| `like stardew valley but not multiplayer` | `multiplayer=False` | `Multiplayer` |
| `like resident evil but nothing scary` | exclude `Horror` | `Horror, Survival Horror` |

SQL deleted a category while the vector hunted for it. The reference game's tags
describe THE GAME; they are not the REQUEST.

**Isolating them** with a hand-built `ParsedQuery`: defect 1 is the whole
reported bug (adding `multiplayer=False` evicts every multiplayer title), while
defect 2 only reshuffles ranks. Worth fixing because it is indefensible, not
because it was expensive.

**Fix**, both in code, prompt untouched: `wants_singleplayer()` - EN and DE,
negation-guarded, filling only and never overriding, with no
`wants_multiplayer()` because "no multiplayer" contains "multiplayer" - and
`_contradicts_filters()` filtering borrowed tags against `excluded_tags` and the
multiplayer axis.

**What this does NOT fix.** `_contradicts_filters` is EXACT match: excluding
`Horror` still borrows `Survival Horror`, because a substring rule would make an
excluded `Action` drop `Action RPG`. And the prompt is still full - this is a net
under the defect, not a repair.

**The part that should have caught this.** Nothing could: there was no
singleplayer case anywhere in `compare_parsers.py` or `queries.yaml`. Both cases
are now in `compare_parsers.py` - a harness cannot catch a regression it never
exercises.

**And the regression check found something else.** Exactly one query moved, one
the change provably cannot reach: re-parsed five times it gives the same tags
every time, so the BASELINE parsed different tags. **The parser is deterministic
within a run and not across runs** at temperature 0, so `--parse` has a floor of
its own and a one-query difference is not evidence of anything.

---

### 33. Every field was optional, so the model just stopped emitting them (2026-09-07)

**Reported.** `...but no wars on linux under 30$` extracted the platform and the
exclusion but no price. The suspicion was the `$` sign - wrong, and cheap to
kill: each notation alone parses fine, and inside the full query ALL FOUR fail
identically.

**Root cause: `_llm_schema()` marked every field optional but `semantic_query`.**
Pydantic puts a field in `required` only when it has no default, and `format`
compiles an optional property into a grammar branch the model may skip:

```json
{ "semantic_query": "game that feels like call of duty",
  "excluded_tags": ["War"], "platforms": ["linux"], "max_required_age": null }
```

`max_price_usd` is **absent**, not null - identical downstream to "no price
requested". The model understood perfectly well: it stripped "under 30$" out of
`semantic_query`. It just never emitted the key.

**Two things this had been hiding.** The field-order invariant was already broken
(with optional properties the model emitted `semantic_query` FIRST, the exact
failure declaring it last was meant to prevent), and it was systemic - the
shipped schema dropped `required_tags` on **64%** of parses against 7% when
required.

**Then the obvious fix made recall worse**, because forcing the fields makes the
model emit more tags and every extra tag was another `@>` conjunct. That exposed
the second defect:

| tag filter | overall | core | specific | tail | under-delivered |
| --- | --- | --- | --- | --- | --- |
| `@>` all-of | 55.4% | 11.1% | 72.7% | 68.2% | 6 |
| `&&` any-of | 60.0% | 16.1% | 79.5% | 70.5% | 0 |
| no tag filter at all | 61.3% | 17.8% | 79.5% | 72.7% | 0 |

Of the 72 queries that got tags, ANDing **helped 3 and hurt 11** - and all three
it helped carried exactly one tag. `running a bookshop and taking on cosmic
horror` returned **zero rows**: nothing carries `Cozy` AND `Horror` AND
`Investigation`. With `&&`, 8,544 games do.

**The instrument could not referee this.** Only 7 of the 118 queries contain
anything constraint-like and NONE names a price, platform, year or age, so
`--parse` measures how little the parser does. Proof it is blind rather than
merely unkind: strip the tag arrays out of the schema entirely and `--parse`
scores the no-parse baseline to the decimal - which is also why "delete the tag
filter" tops the table above and is still the wrong answer.

**A third defect, created by the first fix.** Making `platforms` required means
the model must emit the key, and on a query naming no OS it sometimes fills all
three. Platforms are ANDed, so that silently demands Windows AND macOS AND Linux.
Guarded by `_drop_invented_platforms()`, which only fires when the query names no
OS and can only widen results. Leaving `platforms` optional is worse and was
measured: the key then goes missing on `on linux under 30$`.

*Later correction:* disabling the guard to prove the parser eval would catch this
did not reproduce the invention at all - 0 of 4 on the query that had given 3 of
3. So "3/3 deterministic" was true within one run and not across sessions, which
is this entry's own lesson applied to itself. The harness's ability to catch it
is UNPROVEN. My first check also missed the bug entirely, because I counted
invented SCALARS and never looked at the one array in the required set: a
negative result is only as wide as the fields you looked at.

**Result**, against a baseline-equivalent run in the same session:

| | baseline | with both | delta |
| --- | --- | --- | --- |
| overall | 55.8% | 60.0% | +4.2 |
| `specific` | 72.7% | 84.1% | +11.4 |
| `tail` | 68.2% | 65.9% | **-2.3** |

Not better on every tier: `specific` carries the whole gain and `tail` is down
2.3, exactly the floor at n=44. The first draft of this entry claimed "+5.9 and
better on every tier" from one run against an older baseline.

**And that spread is itself a finding.** #32 put the `--parse` floor at "at least
one query wide". It is wider: four queries moved between two runs of identical
code, and chasing them cost two wrong diagnoses - both stories told about noise.
A `--parse` difference under ~3.5 points is not a result.

**What to take from this.**

1. **An optional field in a constrained-decoding schema is an invitation to omit
   it.** `format` guarantees the output VALIDATES, never that it is COMPLETE.
2. **"The prompt is full" was over-applied.** #13, #22 and #32 all blamed prompt
   saturation for a vanishing filter; this mechanism explains the same symptom
   and is a one-line schema property. Check what the grammar permits first.
3. **An eval made of pure descriptions cannot price constraint extraction.**

---

### 34. The constraint became a filter and stayed in the query vector anyway (2026-09-07)

**Found while answering a different question.** A query asking for a popular game
parsed to `min_reviews: 1000` and `semantic_query: 'cat exploring city or ruins
popular'`. The word had already become SQL and was still being embedded, where it
says nothing about what a game IS and can only match noise. **It leaked on 8 of
the 9 queries that asked for it.**

This is #32's lesson in a new place: **a constraint converted into a filter must
stop influencing the embedding**, and nothing was enforcing that as a rule.

**A second bug in the same regex, found by testing the German side.** `_POPULAR`
listed `bekannt\w*` with a wildcard but `beliebt` bare, and nobody writes the
uninflected adjective - so `beliebte`, `beliebten`, `beliebtesten` fired NOTHING.
The fix is one wildcard; `beliebig` is safely excluded because it diverges before
the `t`, which was checked rather than assumed.

**Fix.** `_strip_popular()` reusing `_POPULAR` itself so trigger and removal
cannot drift, with two guards: skipped when stripping would leave nothing
(embedding "" is worse than embedding a useless word), and run before
`apply_reference` so the regex never sweeps the borrowed tags.

**What it is worth, stated honestly.** Stray moves from cosine rank 20 to 17 and
is still not in the top 10 - RRF at w=0.20 cannot lift a rank-20 result past
rank-1 cosine matches. So this is justified by correctness, not by a number, and
the eval cannot notice: no query in `queries.yaml` contains a popularity word.

**Known limitation.** The words can be content rather than constraint - "play as
a famous detective" now loses "famous". That reading was already wrong before the
change, so this makes an existing misreading slightly worse rather than
introducing a new one.

**Same class, still open, and the claim I first made about it was WRONG.** This
entry originally said `wants_reference_excluded` leaks identically and is
"arguably worse". Measured across 8 phrasings, the excluder survives in **2**,
not 8 of 9 - and what stays behind is a real game name, which is signal rather
than noise. Corrected in #35, which is where measuring it properly led somewhere
much more useful.

---

### 35. A one-word game title could never be recognised (2026-09-07)

**Found by trying to confirm #34's last paragraph, which turned out to be wrong.**
Four of eight exclusion phrasings excluded nothing at all:

```
open world rpg without skyrim               NO REFERENCE FOUND
roguelikes similar to hades, except hades   NO REFERENCE FOUND
racing games like forza but not forza       NO REFERENCE FOUND
```

**Mechanism.** `_word_ngrams` builds 2-to-5 word windows, and its docstring's
claim that "one of them is the game's name" is false for a ONE-WORD name.
`Hades` has 279,741 reviews and no two-word window prefixes it;
`MIN_NAME_LENGTH = 6` blocked it twice over. So this was never an exclusion bug:
it silently broke tag borrowing for every single-word title, which defeats the
entire reason `title_lookup` exists.

**The obvious fix is much worse than the bug**, scored against the 118 eval
queries, none of which names a game - so every hit is a false positive:

| config | titles found | false positives |
| --- | --- | --- |
| shipped (2-5 word windows, min_len 6) | 0/7 | 4/118 |
| every single word, min_len 5 | 6/7 | **24/118** |
| **cued singles, min_len 5** | **6/7** | **4/118** |

A wrong reference appends six wrong tags to `semantic_query`, so that middle row
is a fifth of ordinary queries actively corrupted: "first person puzzle game"
matched `Persona 5 Royal`, because `person` prefixes `Persona`.

**Fix: a single word counts only after a reference CUE** - `like`, `similar to`,
`excluding`, `wie`, `ohne` and friends. Cued singles are appended AFTER the
longest-first windows, so `excluding call of duty` still resolves to `call of
duty` rather than `call`; that ordering is load-bearing twice, because `phrase`
is also what `wants_reference_excluded` looks for an excluder in front of.
`MIN_NAME_LENGTH` 6 -> 5 was measured across the whole function: false positives
did not move, and the real guard was always `TITLE_MATCH_MIN_REVIEWS`.

**Still broken on purpose.** `without skyrim` finds nothing, and should: the real
name is `The Elder Scrolls V: Skyrim`, so no prefix of any query opens it (#23).

**What to take from this.** The bug was invisible because it fails silently and
in the direction of doing LESS. It was only found by testing a different
hypothesis that turned out to be wrong - two entries in a row now improved by
measuring the thing I was about to assert.

---

### 36. A cross-encoder is worth 5.6 points, and three of the four arms lied first (2026-09-07)

**Why reranking at all, decided by measurement.** Every labelled target's exact
cosine rank, bucketed by whether reranking could reach it:

```
already in top 10                            72    48.6%
rank 11-200   a reranker CAN fix             40    27.0%
rank >200     only retrieval can fix         36    24.3%
```

On the two tiers that can price a ranking change it is lopsided: **all 9 tail
misses and 9 of 11 specific misses are already in the pool** - retrieval found
them and the ordering buried them. It also fixes the ceiling in advance: 112 of
148 targets are in the pool at all, so 75.7% is the most any reranker can produce
here. It killed the hybrid-retrieval idea too: the 34 `core` targets outside the
pool are mostly not bugs.

**Result: `bge-reranker-v2-m3`**, its rank substituted for the cosine rank inside
the SAME rrf sum: 66.1% overall against the 60.5% baseline, +9.1 `specific`, with
the counter-metric flat.

**The finding recall cannot see.** A cross-encoder is WORSE at short genre
labels. For "city builder", the query that justified `w=0.20` at all, bge drops
Cities: Skylines II from rank **1 to 37** - it rewards literal topical match, so a
78-review game named `City Builder` wins - while `core` recall reports 17.8 ->
20.0, i.e. slightly BETTER, because core labels 2-3 games out of hundreds. Read
the probe, not the tier.

**Three of the four arms produced a number before they produced a valid one.**

*gte-multilingual-reranker-base scored a full, clean, plausible table that was
entirely the baseline.* It loads fine and then raises a CUDA device-side assert
on every `predict()`, so all 118 queries degraded to the SQL ordering and printed
60.5 / 17.8 / 79.5 / 70.5 - byte-identical to the control, reading as "no better"
rather than "never ran". Reranking took 16ms for 200 candidates, the only visible
tell. `verify_rerank_model()` had checked that the model LOADS. **Loading is not
scoring.** It now scores a probe pair and rejects CONSTANT scores too, and
`run_eval` refuses to print a table if any query fell back. On CPU the real error
appears - the model's remote code against transformers 5.x. Excluded as
incompatible, NOT as worse.

*Qwen3-Reranker-0.6B scored 8.1% overall, and that is my bug, not its quality.*
The seq-cls conversion still needs the Qwen chat template; handed a bare pair it
emits near-zero logits. Same three-document probe, both sorted correctly:

```
bge      0.8647  0.0002  0.0058     spread 0.8645
qwen3    0.5857  0.4921  0.4647     spread 0.1210
```

Right order, no conviction - fine on an obvious triple, useless across 200
similar games. Recorded as NOT MEASURED; see #37.

**What to take from this.** Every layer failed silently and in the direction of
"still works, just worse": Ollama 404s an endpoint that does not exist, TEI warns
and continues on CPU, torch installs a CPU wheel, gte falls back behind a
valid-looking table, Qwen3 returns numbers with no information in them. Not one
raised. The guard that catches the dangerous one - a measurement contaminated by
its own fallback - did not exist until it had already produced a wrong table.

---

### 37. The reranker I recorded as "not measured" was the best one (2026-09-07)

`_pair_for()` now wraps a pair the way each model expects - the reranker's
version of `_MODEL_PREFIXES`. Same probe, after: qwen3's spread goes 0.121 ->
**0.987**.

**It wins, and by more than bge won.**

| config | overall | core | specific | tail | DE | under 1k | rerank |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `rrf w=0.20` | 60.5 | 17.8 | 79.5 | 70.5 | 42.6 | 74% | - |
| bge fused | 66.1 | 20.0 | 88.6 | 75.0 | 40.7 | 73% | 264ms |
| **qwen3 fused** | **68.8** | **27.2** | 88.6 | **77.3** | **51.9** | 71% | 1,021ms |

**The mechanism is instruction-following, and it is visible rather than
inferred.** bge scores -0.01 on FollowIR - at chance - so it can only answer "how
related are these two texts"; Qwen3 can be TOLD what relevance means. That
predicts it wins exactly where bge was weakest, and for "city builder" it holds
Cities: Skylines II at rank 1 where bge drops it to 37. So the `core` gain is not
a labelling artifact: the tier moved AND the mechanism probe moved with it, the
first time those two have agreed in this project.

**Latency is 4x, and it was nearly recorded wrong.** The first run reported p95
6,234ms against a 1,105ms median, which is not a tail - it is one query. The
first batch of a given SHAPE pays CUDA kernel selection (9,668ms against a 963ms
steady state), and `verify_rerank_model()` was warming with a 2-pair probe, which
does not trigger the same kernels. A warm-up that does not match the real shape is
not a warm-up.

**Correction, same day, after being asked to be sure.** The tables above are
point estimates and I presented them as findings. A paired bootstrap (10,000
resamples) and a sign test say only ONE of the three comparisons survives:

| comparison | diff | 95% CI | sign test |
| --- | --- | --- | --- |
| qwen3 - baseline | +8.3% | **[+2.5%, +14.5%]** | 15-4, p=0.0096 |
| bge - baseline | +5.6% | [-0.1%, +11.9%] | 13-7, p=0.132 |
| **qwen3 - bge** | **+2.7%** | **[-2.1%, +7.6%]** | 11-5, p=0.105 |

**Qwen3 and bge are NOT distinguishable on recall** - not overall, not on `core`,
not on `specific`, not on German. 102 of the 118 queries return identical recall;
the entire difference is 11 wins against 5 losses. #36's headline for bge is
marginal by the same test.

What survives: **reranking beats not reranking.** So the model choice cannot be
made on recall, and the tiebreak is the mechanism probe, which is DETERMINISTIC
rather than sampled - and it costs 4x the latency. On a latency budget bge is the
same recall for a quarter of the cost.

The German result specifically does NOT survive (5-2 on discordant queries,
p=0.227) and remains a reason to build a bigger German set, which was already the
conclusion. See #41.

**What to take from this.** n=118 with ~100 ties has far less power than a
118-query eval sounds like it has. This file's stated floor came from EMBEDDING
reproducibility - re-running the same config - which is a different and much
smaller quantity than the uncertainty in a difference between two configs.
**Reproducible is not distinguishable.** Run the paired test before writing the
table, not after being challenged on it.

---

### 38. The hallucination checker hallucinated, three separate ways (2026-09-08)

**The feature is a one-line "why this matches"; the deliverable is the DISCARD
RATE.** A plausible sentence attached to a real game is the hardest kind of wrong
to notice - it looks exactly like the feature working. So every claim is verified
against `games.tags`: the returned app_id must be one we asked about,
`cited_tags` must be a subset of the game's real tags, and any tag NAMED IN THE
PROSE must be one the game has.

**The first honest number was wrong, and only an audit found it.** The initial
run reported 7.3% over 590 explanations. Printing eight discards next to each
game's real tags showed three were the checker's fault:

```
Garden Life: A Cozy Simulator     PROSE but absent: ['Fishing']
  "It is a Cozy Farming Sim, but does not include Fishing."
Little Witch in the Woods         PROSE but absent: ['Experience']
  "Experience the daily life of an apprentice witch..."
```

Three false-positive classes, all inflating the rate:

1. **Overlapping tags.** `Farming` and `Farming Sim` are both real, so "it is a
   Farming Sim" matched BOTH. This fired on the very first live run - two of five
   CORRECT explanations discarded. Fixed by resolving longest-first.
2. **Negation.** The prompt tells the model to hedge rather than invent, so "but
   does not include Fishing" is the model OBEYING. Fixed with a guard scoped to
   the tag's own clause, so "not a puzzle game but it is Souls-like" still flags
   `Souls-like`.
3. **Sentence-initial capitalisation.** `Experience` is a real tag and an
   ordinary verb. Single-word tags no longer count sentence-initially; multi-word
   ones still do.

The genuine catches in the same audit were real: `Random Dungeons` (not even a
tag - the model invented the name), `Bikes` for a game tagged `Automobile Sim`.

**Corrected rate: 4.6%**, down from 7.3%, with the prose check falling from 19
discards to 3 - so **16 of the original 43 were the checker's fault**. The two
untouched checks are bit-identical across runs, which is what says the change did
what it claimed and nothing else.

**The self-test is the reason any of this is trustworthy.** Four known-bad
responses plus a CONTROL with a real response, because a verifier that rejects
everything would otherwise score perfectly. All four arms were caught before the
first real number was taken, which is what made the false positives findable: the
checker was known to fire correctly on lies, so a suspicious rate had to be
investigated rather than explained away.

**What to take from this.** A verifier is a measuring instrument and gets
measured like one. Every one of these bugs pushed the number UP - the
safe-looking direction, which nobody audits. The reported number is also a FLOOR:
it catches invented TAGS, and a model that invents a plot detail passes.

---

### 39. The relaxation ladder's important half is the part that does nothing (2026-09-08)

**It runs on COUNTS, not retried searches, and that is the whole cost argument.**
Re-running `search()` costs ~1.1s of cross-encoder per attempt since #36. A
capped count over the same `_apply_filters()` is 11-24ms:

```
no filters            capped count 200    24ms
tags+price+platform   capped count 200    13ms
very selective        capped count   2    11ms
```

The cap matters: uncapped, the unfiltered count is 611ms, because "how many" is a
much harder question than "are there at least ten".

**The never-relax list is the important half.** A short page is a disappointment;
a confidently wrong page is a defect. Never relaxed: `max_required_age` (a safety
constraint), `excluded_tags` (shows horror to someone who said nothing scary),
`excluded_app_ids` (returns the game they excluded by name), `multiplayer` (the
wrong KIND of game, #32 by another route) and `platforms` (a compatibility fact).
Verified as BEHAVIOUR: a query setting all five plus a price and a year relaxed
ONLY `released_after`.

**No model is in the loop, and that is the point.** An agent would ask the LLM
which constraint to drop. This asks a table, in a fixed order, with a stopping
condition - reproducible, testable, free, and unable to invent a constraint that
was never there.

**The ladder order is a JUDGEMENT and is labelled as one.** There is no eval for
"was that the right constraint to give up", so it is stated as judgement in the
code rather than dressed up as tuned.

**Two things the first test run got wrong, both mine.** The first "starved" query
was not starved at all - `Cozy` AND `Horror` AND `Investigation` has been ANY-of
since #33 - and I briefly read that as a bug in the relaxation. And the notes
used em dashes, which a Windows console renders as a replacement character.

`run_eval` sets `relax_filters=False` and prints `relax: OFF`: a harness that
quietly widened filters whenever a query returned little would report relaxation
as retrieval quality.

---

### 40. A p95 guard that permitted exactly what it forbade (2026-09-08)

**The deliverable of observability is not the dashboard - it is that the numbers
cannot be quoted wrongly.** So p95 is withheld below a threshold. I set it to 20
by eye and asserted p95 was non-null past it. That assertion passed. What it
printed did not:

```
n=20      p50=110.0 p95=119.0 max=119.0  (p95 appears)
```

p95 and max are the same number - the guard was returning the maximum under a p95
label, and the assertion never noticed because it only checked for non-null.

**Mechanism.** Nearest-rank is `ordered[min(n - 1, int(n * 0.95))]`, and that
index equals `n - 1` for every n up to 20. The first n at which p95 stops being
the maximum is **21**:

```
n=  20  p95 index= 19  last index= 19  == MAX
n=  21  p95 index= 19  last index= 20  ok
```

**Fixed** by `MIN_P95_SAMPLES = 21` plus two assertions that make the property
structural: `p95 < max` at the threshold, and a derived check that recomputes the
boundary from the formula.

**What to take from this.** #38's lesson in a new place: a guard that has never
been SHOWN to fire is not known to work, and the way to show it is to print the
evidence beside the thing it is supposed to differ from. Asserting "p95 is not
null" tested that the code ran; printing p95 next to max tested what it meant.

A second, smaller version in the same session: with a deliberately broken
`CHAT_MODEL`, a single search reported `parse_call_failed: 2`. Both were real -
`_warm_models()` parses at startup - but the count is of parser CALLS, not
requests, so dividing it by `search.n` would give a rate above 100%.

---

### 41. The German gain was never there, and it took 13x the sample to say so (2026-09-08)

**The claim under test.** `Qwen3-Reranker` appeared to move German `specific`
recall from 55.6% - a number two embedding models had left byte-identical - to
77.8%. It was the only thing in the project that had ever moved German, and #37
recorded it as failing a paired test at n=9.

**The set.** `eval/queries_de.yaml`: the same 118 targets and tiers asked in
German, generated from the source file rather than written out, because 148
`expect` entries retyped by hand would produce a typo that looks exactly like a
recall failure. `specific` German went from 9 queries to 44.

**The answer, and it is a negative.**

```
                 rrf     rerank    difference            sign test
overall n=118   52.5%    56.4%    +3.8% [-3.0, +11.0]   13W 7L 98T  p=0.263
specific n=44   59.1%    68.2%    +9.1% [-2.3, +20.5]    6W 2L 36T  p=0.289
```

Against **+8.3% [+2.5%, +14.5%]** for the same change on the mixed set. The point
estimate barely moved between n=9 and n=44 - it is not that the effect shrank, it
is that it was never separable from noise, and only the larger sample makes that
a finding rather than a shrug.

**The set also produced the first EN/DE number worth quoting.** Every previous one
was confounded twice: tier mix, and targets pointing at different games. Holding
both fixed leaves language as the only variable, over 91 matched pairs:

```
overall  n=91   EN 73.8%   DE 57.7%   -16.1% [-25.8, -6.8]   4W 20L 67T  p=0.002
specific n=35   EN 91.4%   DE 65.7%   -25.7% [-42.9, -8.6]   1W 10L 24T  p=0.012
tail     n=36   EN 80.6%   DE 69.4%   -11.1% [-25.0, +2.8]   2W  6L 28T  p=0.289
```

67 of 91 tie, so the whole result rests on 24 queries, which an unpaired
comparison of two averages would have hidden completely. *(Re-measured at
ef_search 800 in #42: the overall gap survives at -15.2%, and the per-tier
reading does not - `tail` also excludes zero there.)*

**What to take from this.** "Build a bigger set" is a real answer to an
underpowered result, and it has to actually be built - the claim sat marked *do
not quote* for a whole weekend, which is how a deferred number turns into a
believed one. And the negative is worth more than the positive would have been:
it says the next thing to try for German is a German document field, not a better
reranker.

**A smaller thing found on the way.** The recorded `p=0.105` for Qwen3-vs-bge is
the ONE-SIDED tail of an 11-5 split; two-sided it is 0.2101. Both are correct for
the same data and nothing said which convention was in use. `eval/paired.py` is
two-sided, and its self-test asserts both numbers so they can never be silently
compared.

---

### 42. The published recall was one draw from a non-deterministic index build (2026-09-19)

**Found.** A fresh-clone check reported the real database at **67.1%** overall
against the README's 68.8%. Nothing in the recall path had knowingly changed.

**Ruled out, each with a measurement rather than an argument:**

| suspect | test | result |
| --- | --- | --- |
| Ollama 0.33 -> 0.34 upgrade | re-embed the first 256 rows exactly as `embed_all` did | bit-identical to stored |
| pgvector | image date and extension version | unchanged |
| libraries | `uv.lock` diff since the README commit | no torch/transformers change |
| code | diff of the recall path | dead-code removal and `--dump` plumbing only |
| reranker batch, VRAM pressure | 128 vs 32, contended vs not | identical per query |

**The cause.** An accidental rebuild of the real index. Migration 0007 builds it
with four parallel workers, and **a parallel HNSW build is not deterministic**:
on a copy of the same database two rebuilds gave 67.9% and 66.8%, and the fresh
clone's own build gave 68.5%. Five builds of identical vectors, five answers. A
single-worker build IS repeatable but takes 84s instead of 25s and fixes only
reproducibility.

**Why it could matter at all: `ef_search` equalled the pool.** At 200 the index
is asked for exactly the 200 candidates the reranker needs, with no slack, so
which targets fall off the edge depends on the graph. Swept over three
independent builds:

| overall recall | ef 200 | 400 | 600 | 800 | 1000 |
| --- | --- | --- | --- | --- | --- |
| real index | 67.1 | 69.2 | 71.8 | 71.5 | 71.5 |
| rebuild A | 67.9 | 70.1 | 71.8 | 71.5 | 71.5 |
| rebuild B | 66.8 | 69.2 | 71.8 | 71.5 | 71.5 |
| builds identical per query | no | no | yes | yes | yes |
| queries differing from 1000 | 7 | 4 | 1 | 0 | - |

**800 ships, not 600.** 800 is the smallest value where every build agrees on
every query AND every query matches 1000 - on the German set too. 600 scores 0.3
higher, and that is exactly the reason not to take it: its one remaining
difference from 1000 is approximate search happening to help, and choosing a
setting because the approximation flattered it is the #26 mistake in a new place.

Against 200 on the real index: EN +4.4% [+1.0, +8.5], 6 wins to 1, p=0.125;
`tail` +9.1%; DE +2.5%. The interval clears zero and the sign test does not, so
the case is the MECHANISM - near-exact and build-independent - with the recall as
support, and it is quoted that way. The counter-metric did not move. Cost:
filtered SQL median 16 -> 23ms.

**What this corrects.** CLAUDE.md said "query time and the HNSW build are both
byte-deterministic; the vectors are not." The vectors are reproducible at a fixed
`num_batch`, and the build is not. #31's own rebuild check did match on 09-06 and
I do not know why it held then - recorded as unknown rather than explained. Every
headline number was re-measured at 800; the historical model, weight and reranker
comparisons were taken at 200 and are labelled so.

**What to take from this.** A benchmark number is a function of every
non-deterministic step that produced its inputs, and an index build is one. The
two checks that would have caught it are cheap: rebuild twice and diff, and ask
whether a retrieval parameter sits exactly at the size of the thing it retrieves.
