# Queries that fail

Weekend 1 output, per BUILD_PLAN.md: type in vibes, write down what does not
work. These become the Weekend 3 eval set (`eval/queries.yaml`), so each entry
records what was asked, what came back, and why.

Baseline: `nomic-embed-text`, pure vector similarity, `total_reviews > 10`.

---

## 1. Negation is ignored entirely

**Query.** `game that's similar to call of duty, but we shooting plants,
that's not plants vs zombies`

**Got.** Positions 1, 2 and 3 were all Plants vs. Zombies titles. Position 7
was Call of Duty: Infinite Warfare.

**Why.** The query is embedded as a single vector. The string "plants vs
zombies" is present, so the vector lands near it; "not" barely moves it.
Bi-encoder embeddings have no mechanism for exclusion. The retrieval is
correct — the request is not expressible.

**Fixed by.** Weekend 2's query parser, which lifts negations out of the text
into `excluded_tags` before the remainder is embedded. Note the current
`ParsedQuery` schema excludes *tags*, not *titles*, so "not Plants vs Zombies"
specifically still would not be handled. Worth deciding whether the schema
needs `excluded_app_ids` or a title-exclusion field.

---

## 2. Proper nouns as similarity anchors

**Query.** Same as above — "similar to call of duty".

**Why.** "Call of Duty" as a token carries little semantic weight compared to
words like "military" or "shooter". Vector similarity cannot reliably use a
named title as a reference point.

**Fixed by.** Weekend 4's hybrid sparse + dense retrieval. BUILD_PLAN.md flags
this exact case: keyword search finds the literal title, RRF fuses the two
rankings. Expected to be one of the larger wins in the results table.

---

## 3. Hard constraints are treated as vibes

**Query.** `co-op game under 20 dollars that runs on linux`

**Got.** Windows-only games in the results. Price and platform ignored.

**Why.** "linux" and "under 20 dollars" are embedded as *text*, so the query
matches games whose descriptions read like cheap Linux co-op games. Similarity
has no access to `games.linux` or `games.list_price_usd`, even though both are
populated, correct and indexed.

This is the single clearest argument for the whole project's hybrid design:
the right answer is one WHERE clause away, and pure semantic search cannot
reach it.

**Fixed by.** Weekend 2. The parser turns this into
`platforms=["linux"], max_price=20, semantic_query="co-op game"`; SQL applies
the first two, pgvector ranks what survives.

**Note for Weekend 3.** Once filters exist, this becomes a high-value eval
query: correctness is objectively checkable (every result must have
`linux = true` and `list_price_usd <= 20`), unlike vibe queries where relevance
is a judgement call.

## 4. Titles outweigh tags

**Query.** `very easy relaxing game with no challenge`

**Got.** Two of the top ten are tagged `Difficult`: *EasyPianoGame* (#7,
tags `Casual, Rhythm, Arcade, Difficult, Replay Value`) and *Ease Out* (#8,
tags `Rhythm, Casual, Difficult, 2D, Early Access`). *No Time to Relax* (#2)
is a party game about stress.

**Why.** `embed_text` is `{name}. {short_description} Tags: ...`. The name
leads and is short, so it carries disproportionate weight against fifteen
tags. A game called *EasyPianoGame* reads as being about ease.

**Worth measuring in Weekend 3.** Add rows to the results table for embed_text
variants: name last instead of first, name repeated, tags weighted by
duplicating the top few. This is cheap to test — re-embedding is ~12 minutes —
and it changes every result in the system.

**Note.** The antonym test this came from otherwise *passed*: "brutally
difficult" and "very easy relaxing" returned completely disjoint lists.
Polarity is carried by the tags (`Difficult` vs `Relaxing`), which is direct
evidence for the tags-in-embed_text decision.

---

## 5. Self-description beats reputation

**Query.** `brutally difficult game that will make me suffer`

**Got.** Almost entirely long-tail games — 21, 31, 28, 18, 14 reviews. Only
one result above 5,000 reviews. No Dark Souls, Getting Over It, Celeste or
Super Meat Boy.

**Why.** Obscure games literally write "This game is hard" in their
description. Famous difficult games describe their world and tone; their
difficulty is reputation, carried in reviews and culture rather than in the
120 characters we embed. The loudest self-describers win.

**Open question.** Does raising the review threshold surface the canonical
games, or are they genuinely unreachable by this query? Testable now with
`--threshold 5000`, and a natural row in the Weekend 3 recall table.

**Possible fixes.** A popularity term in ranking (Weekend 4 territory, and
BUILD_PLAN.md warns against unmeasured additions), or the reranker, which reads
query and game together and may weigh reputation cues better.

## 6. Player context read as game content

**Query.** `something I can play one-handed while eating`

**Got.** Games *about* hands and food: Don't Cut Your Hand, Hungry Tiger,
I'm Hungry, Lunch A Palooza, Eat Your Words.

**Why.** The query describes the player's situation. Every word of it becomes
a topic to match against game descriptions. Nothing distinguishes "a game I
play while eating" from "a game about eating".

**Hard to fix.** Weekend 2's parser could in principle map this to
`required_tags=["Casual"]` plus a controls constraint, but "one-handed" is not
in Steam's tag vocabulary at all. Honest answer for the README: some intents
are not expressible in this data.

---

## 7. Gaming jargon read literally

**Query.** `game where the map changes every run`

**Got.** Worm Runner, Speedrun, RUN: The World In-Between, and Fantasy Map
Simulator — a map *creation* tool. No roguelikes: no Hades, Dead Cells, Slay
the Spire, Binding of Isaac.

**Why.** "Run" means a playthrough in games and locomotion in general English.
"Map changes" was read as editing maps. The query describes procedural
generation without using the term.

**Related.** `time loop where you replay the same day` drifted to
time-*manipulation* games (rewind mechanics) and missed Outer Wilds, Deathloop,
Twelve Minutes. Adjacent concept, wrong one.

**Fixed by.** Weekend 2's tag grounding: the parser sees the real vocabulary,
so "map changes every run" can become `required_tags=["Roguelike",
"Procedural Generation"]`. This is the strongest argument for grounding the
parser in the actual 452 tags rather than letting it invent.

---

## 8. Number tokens in titles, and an unsafe result

**Query.** `game for a 7 year old that isn't violent`

**Got.** #1 Paintball 707 (a first-person shooter). #5 Knockout Daddy, tagged
`Violent`. #9 Chill, tagged `Nudity`. #2 "Seven boys 2" and #4 "7 Years From
Now" matched on the digit 7.

**Why.** Negation ignored (see #1), plus title-token matching (see #4). The
digit 7 is a strong literal signal with no semantic connection to a child's
age.

**Schema gap this exposed.** The source JSON carries `required_age` and the
`games` table does not have it. That is the column that makes this query
answerable, and omitting it was a mistake in the 0001 schema design. Add it in
a Weekend 2 migration alongside the tags array.

**Worth a README paragraph.** Recommending a game tagged `Nudity` to someone
shopping for a seven-year-old is the kind of plausible-looking wrong answer
this project is supposed to be about.

---

## 9. Social constraints ignored

**Query.** `something to play with my mum who has never gamed before`

**Got.** Mostly singleplayer educational and puzzle games aimed at children.

**Why.** Two failures at once. "Play *with*" implies co-op or local
multiplayer — `Co-op` and `Multi-player` are loaded in `game_categories` and
went unused. And "never gamed before" was conflated with "for children"; a
beginner adult is not a child.

**Fixed by.** Weekend 2: `multiplayer=true` maps onto game_categories. The
beginner-vs-child conflation is a genuine semantic limitation and worth
keeping as an eval query.

---

## Works well (calibration)

Not everything fails, and the eval set needs positive cases too.

**`game with a grappling hook that feels great to swing`** — all ten results
are genuine grappling-hook games, top score 0.802, the highest seen. Specific
mechanics that appear verbatim in descriptions work well.

Caveats even here: #7 *Hook* is a minimalist puzzle game that matched on title,
and no canonical examples (Just Cause, Spider-Man, Titanfall) appear — the same
reputation-versus-self-description problem as #5.

---

## Resolution of #5: crowded out, not unreachable

`brutally difficult game that will make me suffer --threshold 5000` returns
Getting Over It at #1 ("A game I made for a certain kind of person. To hurt
them."), plus Darkest Dungeon, Thymesia and DDNet.

So the canonical hard games are reachable — 130k long-tail entries were simply
ranked in front of them. The threshold is doing more work than "filter out
asset flips"; it is choosing between long-tail discovery and canonical answers.
Good argument for exposing it in the UI rather than fixing it at one value.

Still absent even at 5000: Dark Souls, Elden Ring, Sekiro, Celeste, Cuphead.
Their descriptions sell setting and tone; difficulty lives in their reputation.
Reputation is not in the 120 characters we embed.

---

## 10. "Brutal" reads as gore, not as challenge

**Query.** Same, at threshold 5000.

**Got.** Roughly half the list matched violence rather than difficulty: Blood
Trail (`Gore, Violent, VR`), Bloody Hell (matched "Bloody" to "brutally"),
Dark Deception (horror), the static speaks my name (dark exploration), and
Nothing (`Hentai, Sexual Content`).

**Why.** "Brutally" and "suffer" are closer in embedding space to violence and
horror than to mechanical difficulty. The intended sense — hard to play — is a
narrower reading than the general-English one.

**Fixed by.** Weekend 2 tag grounding: this maps cleanly onto the real tag
`Difficult`. Another vote for grounding the parser in the actual vocabulary.

---

## 11. Selective filters cost 10x latency

**Observed.** Query time went from ~150ms at `--threshold 10` to **1446ms** at
`--threshold 5000`, same query.

**Why.** `hnsw.iterative_scan` re-scans the index until LIMIT is satisfied.
With 2,732 of 130,651 rows passing, it scans a lot. This is the price of the
correctness fix in app/search.py — without it the query silently returns four
results instead of ten.

**Correction to an earlier measurement.** The 2.8ms figure recorded when
choosing iterative_scan used an existing row's embedding (Stardew Valley) as
the query vector — a point already in the index with dense neighbours. A real
query embedding lands in sparser space and costs far more. Benchmark with real
queries, not with rows already indexed.

**Watch in Weekend 2.** Stacked filters (price AND platform AND tags) will be
far more selective than this. If latency becomes a problem, the options are
raising `hnsw.max_scan_tuples`, pre-filtering to app_ids and searching within
them, or accepting `relaxed_order`. Measure before choosing.

**Measured.** `"co-op base builder" --platform linux --max-price 20
--multiplayer` filters to 1,867 of 130,651 rows (1.4%) and takes **417ms**.
Acceptable for now. Note that a probe using an existing row's embedding
suggested 19.6ms — optimistic by 20x, because that vector's neighbours already
matched the filters. Real query vectors land in sparser space.

---

## Resolved by structured filters (2026-08-29)

`app/search.py` now takes a `ParsedQuery` and applies price, platform, tag,
year, age and multiplayer filters in SQL before pgvector ranks what survives.
Driven by CLI flags for now; the parser fills the same object next.

**#3 (hard constraints) — fixed.** `"co-op base builder" --platform linux
--max-price 20 --multiplayer` returns Stellar Settlers, Necesse, Volcanoids and
similar: every result Linux, every price under $20, all genuinely co-op.

**#8 (child safety) — the tools now exist.** `--max-age 7` plus
`--exclude-tag Violent --exclude-tag Nudity --exclude-tag Gore` can express the
query that previously returned a first-person shooter and a game tagged
`Nudity`. Note `required_age` alone is insufficient: only 1,321 of 138,964
games carry a non-zero value, so the tag exclusions do most of the work.

**#9 (social constraints) — partly.** `--multiplayer` maps onto
`game_categories`, using the full co-op set rather than `Multi-player` alone
because 744 of 22,127 co-op games lack that category. The
beginner-adult-vs-child conflation is unaffected; that is a semantic limit, not
a missing filter.

**#7 (jargon) — partly.** `--tag Roguelike` reaches games that "map changes
every run" could not. Turning the phrase into the tag is the parser's job.

**Still open and unfixed by this step:** #1 negation in free text, #2 proper
nouns, #4 titles outweighing tags, #5 reputation vs self-description, #6 player
context, #10 "brutal" reading as gore.

---

## Query parser results (2026-08-29)

`qwen3.5:9b` vs `qwen3.5:4b` over the ten queries in `eval/compare_parsers.py`,
three of them German. Ollama `format` with `ParsedQuery.model_json_schema()`,
`temperature=0`, `think=false`. Two rounds of prompt iteration; each row below
is a real defect the harness caught, not a hypothetical.

Round 1 produced **10 disagreements out of 10 queries**. At temperature 0 that
is not model variance — it means the prompt underconstrained the task.

### 12. Schema field order is reasoning order

**Got.** `semantic_query` returned as the *entire original sentence*, unstripped,
on 4 of 10 queries. Filters were extracted correctly alongside it.

**Why.** Ollama's `format` constrains generation, so fields are emitted in
schema declaration order and an earlier field cannot be revised once written.
`semantic_query` was declared first, so the model had to write the
constraint-stripped query *before* extracting any constraints.

**Fixed by.** Moving `semantic_query` last in `ParsedQuery`. It is the only
field whose value depends on all the others. Comment in `app/schemas.py` marks
the ordering load-bearing.

### 13. Prompt layout is load-bearing, and the two blocks compete

**Got.** With the ~1,400-token tag vocabulary at the *bottom*: tags extracted
well, but price/platform/year ignored — the headline query lost both
`max_price=20` and `platforms=["linux"]`. Moving the vocabulary to the *top*
fixed the scalars and collapsed the tags: `"cozy farming game with fishing"`
went from three tags to zero.

**Why.** Whichever block sits nearest the query wins the model's attention.
Recency is the strongest lever in the prompt, and it only points one way at a
time.

**Fixed by.** Vocabulary back at the bottom, *plus* rewriting the scalar rules
so they hold on wording rather than position, *plus* removing a blanket "no
filter is better than a wrong one" line that was suppressing tags. The
anti-inference rule is now scoped to the five scalar fields only. Both
extractions now pass together — but any future edit here needs
`compare_parsers.py` re-run, not just a read.

### 14. Mood read as player count

**Got.** `multiplayer=false` invented on 6 of 20 parses — "something like dark
souls", "cozy farming game", "entspanntes Spiel zum Abschalten". Nothing in
those queries mentions playing alone.

**Why.** Both models inferred solo play from mood words. A wrong `multiplayer`
is worse than a missing one: it silently removes every co-op game.

**Fixed by.** An explicit prompt rule naming "cozy", "relaxing", "entspannt"
and "gemütlich" as saying nothing about player count. 6 occurrences to 0.

### 15. Vague price words invented a number

**Got.** "cheap relaxing puzzle games" produced `max_price_usd=20` on 9b and
`max_price_usd=0` on 4b. The `0` is the dangerous one — it restricts to free
games only, silently.

**Fixed by.** A price now requires a NUMBER, or the word free/kostenlos.
"cheap", "günstig", "budget" stay in `semantic_query` where the embedding can
use them.

### 16. `released_after` missed by 9b — OPEN

**Query.** `free multiplayer shooter released after 2020`

**Got.** 9b returns `max_price=0` and `multiplayer=true` but no
`released_after`. 4b gets it, reproducibly, in all three runs.

**Why.** Unknown. The rule is stated plainly and the field sits sixth of nine
in schema order. This is the clearest single win 4b has over 9b.

### 17. Co-op collapses into `multiplayer`, losing the tag — OPEN

**Query.** `co-op base builder under 20 dollars that runs on linux`

**Got.** `max_price=20`, `platforms=["linux"]`, `multiplayer=true` — but
`required_tags` empty, on both models, despite the worked example in the prompt
naming `["Co-op", "Base-Building"]` for this exact sentence.

**Why.** Probably refusal to double-encode: once "co-op" became
`multiplayer=true`, the model treats the `Co-op` tag as redundant, and leaves
"base builder" for the embedding. Defensible, and pgvector does recover it —
but `Base-Building` is an available hard filter going unused.

### Model comparison

| | qwen3.5:9b | qwen3.5:4b |
|---|---|---|
| "cheap relaxing puzzle, nothing scary" | `Puzzle Relaxing not Horror` | `(none)` |
| "entspanntes Spiel zum Abschalten" | `Relaxing` | `(none)` |
| `semantic_query` stripping | "base builder", "fun game" | keeps constraint words |
| "released after 2020" | misses (#16) | gets it |
| warm average | 0.73s | 0.59s |

**Chosen: `qwen3.5:9b`.** 4b returns no filters at all on two of ten queries,
one of them German. 9b's failures are single omissions. The 0.14s sits in front
of a ~417ms search, so end to end it is ~1.1s vs ~1.0s — not worth two dead
queries.

Selection was on evidence, not size: 4b beat 9b on #16 and on `Souls-like`, and
was a live contender until the two `(none)` rows decided it.

### 18. `Remote Play Together` counted as multiplayer

**Query.** `co-op base builder under 20 dollars that runs on linux --parse`

**Got.** Position 8 was HEXAROMA: Village Builder, whose full category list is
`Single-player, Remote Play Together, Family Sharing, Save Anytime, ...` - no
multiplayer category of any kind.

**Why.** `MULTIPLAYER_CATEGORIES` in `app/search.py` included
`Remote Play Together`, which is a *streaming* feature: it sends one player's
screen to a friend. A Single-player game qualifies for it. Measured against the
corpus:

    matched by the filter      23,750
    genuinely multiplayer      23,113
    false positives (RPT only)     637

**Fixed by.** Dropping that one entry. The other six still cover the case the
wide list was built for - 744 of 22,127 co-op/PvP games lack `Multi-player`
itself, and those are unaffected.

**Worth noting how this was found.** `eval/compare_parsers.py` could never have
caught it: it inspects the `ParsedQuery` and stops there. The parser was
entirely correct here - `max_price=20`, `platforms=["linux"]`,
`multiplayer=true`, `semantic_query="base builder"`. The filter underneath it
was wrong. Checking parsed filters and checking returned games are two
different tests, and only the second one found this.

---

## Measured non-fixes (2026-09-04)

Two proposed improvements, both tested before implementation, both wrong. Kept
here because a disproved idea is worth as much as a confirmed one and costs
more to re-derive than to read.

### 19. `embed_text` recipe: no measurable effect — NOT WORTH DOING

**Hypothesis.** CLAUDE.md carried this from Weekend 1: `embed_text` is
`{name}. {short_description} Tags: ...`, so a short name first outweighs the
tags — *EasyPianoGame* (tagged `Difficult`) ranks for "easy relaxing game".
Proposed fix: name-last or repeated tags, "one f-string plus ~12 min
re-embedding".

**Test.** Real `embed_text` from the database for 14 real games — the seven
junk results the Elden Ring query returned, plus seven genuine souls-likes.
Three recipes (current / name-last / no-name) crossed with the nomic
`search_document:` + `search_query:` prefixes, scored against
`"extremely hard to beat game like elden ring"`.

**Result.** All six variants: **1 of 5 relevant in the top 5.** The rankings
barely moved. Removing the name entirely left *Elden: Path of the Forgotten*,
*Elo Hell*, *Elems* and *Elude* above *Hollow Knight*, *Blasphemous* and
*Nioh*.

**Why.** Deleting `{name}` from the f-string does not remove the name from the
embedded text. **32,201 of 130,633 descriptions (25%) begin with the game's own
name**, and 49,398 (38%) contain it somewhere. Steam blurbs open with the title
by convention. The f-string never controlled the thing it was blamed for.

Checked separately on the original *EasyPianoGame* case: no-name drops its
score 0.714 → 0.654, but *A Short Hike* only reaches 0.515. The ranking never
flips. The name was never the deciding factor.

**Also tested: nomic task prefixes.** `nomic-embed-text` is trained with
`search_document:` / `search_query:`, and neither the code nor Ollama's
template (`TEMPLATE {{ .Prompt }}`) supplies them. Adding them nudged real
souls-likes up ~0.03 and changed no ranking. Principled, but not worth a
re-embed on its own — fold it into Weekend 3's `bge-m3` swap, which re-embeds
anyway.

### 20. Embedding-based tag shortlisting — WOULD REGRESS

**Hypothesis.** The prompt carries all 452 tags (~1,400 tokens) and is
demonstrably saturated: #13 records two separate occasions where added text
silently destroyed scalar extraction. Embed the query, keep only the ~30
nearest tags, and the prompt drops to ~100 tokens.

**Test.** Embedded all 452 tags, ranked them against seven eval queries, and
checked where the tags currently extracted actually land.

| query | tag needed | rank of 452 |
|---|---|---|
| extremely hard to beat game like elden ring | `Souls-like` | **133** |
| extremely hard to beat game like elden ring | `Difficult` | **51** |
| cheap relaxing puzzle games, nothing scary | `Horror` | **171** |
| gemütliches Aufbauspiel für zwei | `Cozy` | **354** |
| gemütliches Aufbauspiel für zwei | `Co-op` | **211** |
| entspanntes Spiel zum Abschalten | `Relaxing` | **89** |
| cozy farming game with fishing | `Fishing` | 0 |
| co-op base builder ... on linux | `Base-Building` | 1 |

**Result.** A top-40 shortlist would delete tags that work today. Three failure
classes:

1. **Negation inverts the vector.** "nothing scary" embeds nowhere near
   `Horror` — excluded tags are unreachable by construction.
2. **German collapses.** `nomic-embed-text` is English-centric; `Cozy` at 354
   of 452 is worse than random for the query that needs it.
3. **Franchise references do not work.** "like elden ring" does not embed near
   `Souls-like`, which is the whole point of the query.

It works only for direct English topical mentions, which are the cases already
succeeding. Abandoned.

**What it did reveal.** "elden ring" cannot embed near `Souls-like` — but the
ELDEN RING *row* carries that tag already. That is #21.

### 21. Franchise references, fixed by looking the game up

**Query.** `extremely hard to beat game like elden ring, not including itself`

**Got (before).** ELDEN RING at #1, then *Elden: Path of the Forgotten*,
*Impossible Runner*, *Elo Hell*, *Elude*, *Elems*, *EllrLand*, *Eldegarde* —
games matching the string "Eld", not the meaning. And "not including itself"
was silently dropped: `ParsedQuery` could exclude *tags*, never *titles*, which
#1 has flagged since Weekend 1.

**Why.** Two things at once. The embedding cannot connect "elden ring" to
`Souls-like` — measured 133rd of 452 tags (#20). And the schema had nowhere to
put a title exclusion.

**Fixed by** `app/title_lookup.py`. If a game name appears inside the query and
that game clears `TITLE_MATCH_MIN_REVIEWS`, its top 6 tags are appended to
`semantic_query`, and its `app_id` goes into a new `excluded_app_ids` when the
query asked to leave it out. No LLM, one SQL statement, 1-2ms.

    semantic_query: "game like elden ring.
                     Souls-like, Open World, Dark Fantasy, RPG, Difficult, Action RPG"
    excluded_app_ids: [1245620]

**Got (after).** ELDEN RING NIGHTREIGN, *The Memory of Eldurim*, *Eldegarde*,
**DARK SOULS™ II**, *Trapped Souls*, *Alaloth*, *God Souls*. Every string-match
result gone except *Eldegarde*; real souls-likes in. ELDEN RING itself absent,
NIGHTREIGN correctly kept — a different game.

**The review floor is the whole trick.** Common English words are real Steam
titles: `Nothing` (9,260 reviews), `Something`, `SELF`, `Dollar`, `Beat`,
`Feels`, `GAME`. Without a floor, "nothing scary" matches a horror game called
*Nothing*. At 50,000 all seven test queries produced zero false positives while
still finding ELDEN RING (1,056,677) and Stardew Valley (886,195).

**Tags are appended, not required.** ELDEN RING carries six tags; requiring all
six returns almost nothing and requiring a guessed subset is arbitrary. Biasing
the query vector has no such cliff — worst case it does nothing.

**Still open.** Franchise names with trademark or edition suffixes do not
match: Steam stores `DARK SOULS™: Prepare To Die Edition` and
`Call of Duty®: Modern Warfare`, so the query text is *shorter* than the name
and `query ILIKE '%name%'` fails in that direction. Needs trigram matching, a
`pg_trgm` extension and a migration, and carries real false-positive risk.
Worth its own measurement.

### 22. "popular" had nowhere to go — and the prompt is full, confirmed

**Query.** `I like FPS shooters, suggest some excluding call of duty, which are
also popular`

**Got.** Results between 43 and 6,007 reviews — *FPSBois* (48), *Sniper Game*
(43), *FPS Training* (77). "which are also popular" was stripped from
`semantic_query` and then discarded: `ParsedQuery` had no popularity concept at
all, despite `search()` already taking a `threshold` nothing could reach.

**Fixed by** `min_reviews` on `ParsedQuery`, raising search's floor with
`max(base, min_reviews)` so it can never drop below the baseline quality gate.
`POPULAR_MIN_REVIEWS` in config, not inlined.

**The prompt attempt, and its cost.** The obvious route was a prompt rule. The
two earlier regressions (#13) both came from editing the *tag* block at the
bottom, so the hypothesis was that the *scalar* block would be safe. It was
not. One added rule, measured over 12 queries:

| query | before | after the rule |
|---|---|---|
| game that feels like call of duty ... on linux | `linux  not War` | `linux` |
| rundenbasierte Strategie mit Koop-Modus | `Turn-Based Strategy  Co-op  multiplayer` | `Turn-Based Strategy  Co-op` |

Reverted. Detected in code instead — a regex over "popular", "well-known",
"famous", "beliebt", "bekannt" — and the eval returned to 12 queries, 0
disagreements, with the new query extracting `>= 1,000 reviews`.

**This is the third confirmation that the prompt is saturated**, and the first
that position within it does not matter. `min_reviews` is stripped from the
schema handed to Ollama along with the other code-only fields. The price is
that a stated number — "at least 500 reviews" — is not understood. Accepted:
one unhandled phrasing is cheaper than a filter that silently stops working.

**Still weak, and not fixed by this.** At 1,000 the query returns aim trainers
— *Aim Hero*, *Aimtastic*, *3D Aim Trainer* — because `semantic_query` is
"FPS shooters", two words whose vector sits close to anything literally about
FPS aiming. Requiring the `FPS` tag does not help; those games genuinely carry
it. The threshold is what separates them:

| floor | top results |
|---|---|
| 1,000 | Aim Hero, Tower of Guns, FPS Game: Dev Test, Aimtastic |
| 5,000 | Quake Live, A.V.A Global, S.K.I.L.L., Black Squad |
| 20,000 | SUPERHOT, Rainbow Six Siege, Black Squad |

Two real limits behind that. Short queries produce diffuse vectors where
literal name matches dominate — the same mechanism as #19. And "excluding call
of duty" still does nothing, because `Call of Duty®: Modern Warfare` is longer
than the query text (#21's trigram gap), so the query silently keeps the games
it asked to drop.

### 23. Franchise names — prefix matching, no pg_trgm needed

**Corrects #21**, which recorded this as needing "trigram matching, a `pg_trgm`
extension and a migration, and carries real false-positive risk". It needed
none of those. The diagnosis was right and the proposed remedy was wrong.

**The problem.** `Call of Duty®` and `DARK SOULS™: Prepare To Die Edition` are
*longer* than what anyone types, so `:query ILIKE '%' || name || '%'` — asking
whether the name sits inside the query — fails for exactly the games most
likely to be referenced.

**The fix.** Invert it. Take word windows from the query and ask whether any of
them *opens* a name: `lower(name) LIKE :phrase || '%'`. Plain SQL, no
extension, no migration, no similarity threshold to tune.

| query | matched | before |
|---|---|---|
| ...like call of duty... | **Call of Duty®** (714,114) | *(none)* |
| something like dark souls... | **DARK SOULS® III** (413,775) | *(none)* |
| ...like elden ring... | ELDEN RING | ELDEN RING |
| ...like stardew valley... | Stardew Valley | Stardew Valley |
| nothing scary / 20 dollars / 7 year old | *(none)* | *(none)* |

1-26ms, and the same review floor keeps the false-positive count at zero.

**Exclusion had to be generalised twice.** `wants_reference_excluded` only
matched "not including *itself*" — the oblique phrasing. "excluding call of
duty" names the game again, so it also looks for an excluder within two words
of the phrase that matched.

Then excluding one `app_id` proved insufficient: *Modern Warfare* and *Black
Ops Cold War* stayed in results for a query that said to exclude Call of Duty.
The match is a prefix, so the exclusion is one too — 24 franchise entries
removed rather than one.

**Result for** `I like FPS shooters, suggest some excluding call of duty, which
are also popular`:

    before  Aim Hero, Aimtastic, FPSBois (48 reviews), FPS Training, Sniper Game
    after   Squad (210k), Arma 3 (283k), Battlefield 2042 (297k), Insurgency,
            Battlefield 4, Medal of Honor, Sniper Ghost Warrior 3

Three mechanisms had to work together: the review floor removed the shovelware,
the borrowed tags (`FPS, Multiplayer, Shooter, Military`) pulled the vector
toward military shooters, and the franchise exclusion removed what was asked
for. None of the three alone was enough.

**Known behaviour change.** Prefix exclusion catches sequels and spinoffs:
"like elden ring, not including itself" now drops ELDEN RING NIGHTREIGN too.
Defensible — someone asking for games *like* Elden Ring wants different games —
but it is a judgement call, not a derivation.

### 24. The corpus outranks itself: no quality term in ranking (2026-09-04)

The baseline was 6.1% recall@10 (9.2% EN, 0.0% DE) on `nomic-embed-text`, and
DE at zero made this look like a German problem. It was not: the German queries
are near-parallel to the English ones, so DE's ceiling *is* EN's 9.2%. Both
columns bad pointed at the model.

Replacing the model barely moved it. `snowflake-arctic-embed2` — MTEB Retrieval
55.6, CLEF 54.1, top of the indexable candidates — gave 8.3% overall: DE
unstuck (0.0% -> 10.0%) but **EN down**, 9.2% -> 7.5%.

The score hid what the results made obvious. Top 10 for `city builder`:

    1. City Builder                                   0.577
    2. Megacity Builder                               0.542
    3. 20 Minute Metropolis - The Action City Builder 0.518
    4. Constructor Plus                               0.511
    5. City Block Builder                             0.510
    6. Square City Builder                            0.509
    7. Epic City Builder 4                            0.507
    8. Cities: Skylines II  (73,524 reviews)          0.502

Every game whose *name* restates the query beats the canonical answer. Nothing
is wrong with the retrieval — those are all genuinely city builders. Cosine has
no notion of prominence, and in a corpus that is overwhelmingly shovelware, an
asset flip named literally "City Builder" wins the lexical match every time.
The same shape explains `deckbuilding roguelike` and `metroidvania with tight
combat` missing their obvious answers.

Sweeping `REVIEW_THRESHOLD` confirms it (arctic-embed2, exact scan, no HNSW):

| threshold | corpus | EN | DE | overall |
| --- | --- | --- | --- | --- |
| 10 | 55,120 | 7.5% | 10.0% | 8.3% |
| 100 | 22,700 | 19.2% | 15.0% | 17.8% |
| 1,000 | 7,212 | 26.7% | 25.0% | 26.1% |
| 10,000 | 1,702 | 63.3% | 45.0% | **57.2%** |
| 50,000 | 470 | 45.8% | 50.0% | 47.2% |

7x, from a config value. The peak is not an artifact of the ground truth being
famous games — that would climb monotonically as the corpus shrank. It *falls*
at 50,000, where the filter starts eating expected results (Coffee Talk,
A Short Hike, Monster Train).

**Not adopting 10,000.** It buys 57% by deleting 128,949 of 130,651 games,
which is search over the Steam top 1,700. The long tail is the product — see
the `REVIEW_THRESHOLD=10` convention in CLAUDE.md, chosen deliberately because
the 11-50 review band is 24,762 real indie games.

**What this actually says.** #22 already stumbled on the fix without naming it:
the FPS query was rescued partly by `wants_popular()` applying a review floor,
and that only fires when the user types "popular". Ranking needs a *continuous*
quality term always — blend review count or positive ratio into the score —
rather than a cliff the user has to ask for. That is a search-ranking change,
so it goes through plan mode (CLAUDE.md review discipline, category 2), and a
silently wrong ORDER BY here would produce plausible results forever.

**Corollary for the model comparison.** Comparing embedding models at
`REVIEW_THRESHOLD=10` mostly measures which one is best at matching shovelware
titles. Compare them at a threshold where the signal is visible, and report the
curve rather than one number.

### 25. Three models, and the ranking inverts at the threshold (2026-09-04)

**The claim under test.** #24 ended with a corollary: comparing embedding models
at `REVIEW_THRESHOLD=10` "mostly measures which one is best at matching
shovelware titles," so compare them higher up. Running all three across the
whole curve shows that is half right — and the wrong half matters.

recall@10, exact scan (HNSW dropped, so this is ground truth, not index recall),
per-query average, 30 labelled queries:

| threshold | corpus | arctic-embed2 | bge-m3 | qwen3:0.6b |
| --- | --- | --- | --- | --- |
| 10 | 55,120 | 8.3% | 10.0% | **18.3%** |
| 100 | 22,700 | 17.8% | 10.0% | **22.8%** |
| 1,000 | 7,212 | 26.1% | 22.8% | **26.7%** |
| 10,000 | 1,702 | **57.2%** | 49.4% | 38.3% |
| 50,000 | 470 | **47.2%** | 44.4% | 38.9% |

**The models swap places.** There is a crossover between 1,000 and 10,000:
below it qwen3 leads by 10 points, above it arctic leads by 19. Neither model
is "better." One is more robust to a corpus full of self-describing asset flips,
the other retrieves better once they are gone. #24's corollary assumed the low
threshold measured *noise*; it measures a real and different property, and it
happens to be the property the shipped configuration depends on.

**Consequence for the eval.** A single-number model comparison at one threshold
would have picked either model depending on which threshold was chosen, with no
warning that the other choice existed. Report the curve.

**Leaderboards did not predict any of it.** bge-m3 leads MIRACL by 13 points
(69.2 vs 55.8) and came last or joint-last at four of five thresholds.
arctic-embed2 leads MTEB Retrieval (55.6 vs 48.8) and loses at the threshold in
use. CLAUDE.md named bge-m3 as the Weekend 3 target on exactly that evidence.
The plan predicted the failure mode — MIRACL is monolingual DE→DE, this is a
German query against English documents — and this is the measurement confirming
it. Third falsified hypothesis in this file, after #19 and #20.

**Shipped qwen3**, because `REVIEW_THRESHOLD=10` is the configuration that
ships. This is provisional: the popularity term #24 asks for moves the effective
regime toward the clean-corpus end where arctic wins, so the model choice must
be re-measured after that lands rather than inherited. One `--reload` and one
sweep, ~30 min.

**Method note, from getting it wrong the first time.** Pass 1 was run piecemeal,
with one embed job interrupted and resumed, and bge-m3's pre-eval check used
`min(embedding_model)` — which returns `bge-m3` whether or not arctic vectors
remain in the column, so it cannot detect the one failure
`check_model_consistency()` exists to prevent. Pass 2 re-ran all three from
`--reload` with `count(DISTINCT embedding_model)` plus a zero-vector check
before every eval. All thirty numbers reproduced exactly, which also confirms
the `embedding IS NULL` work queue makes an interrupted run indistinguishable
from a clean one. The verification was genuinely unsound; the results were not.
Both facts are worth keeping — a check that cannot fail is not a check.

### 26. The ground truth has no long tail, so recall bought one (2026-09-05)

**What was measured.** #24 concluded that ranking needed a continuous
popularity term rather than the `REVIEW_THRESHOLD` cliff. Built it as a
two-stage query - HNSW retrieves 200 candidates, stage 2 reranks - with two
blend methods behind a config flag so the eval could choose. It chose badly, and
the way it failed is more useful than the feature.

recall@10 at threshold 10, sweeping the weight:

| weight | log | rrf |
| --- | --- | --- |
| baseline (none) | 18.3% | 18.3% |
| low | 25.0% (0.05) | 25.0% (0.20) |
| mid | 31.1% (0.20) | 31.1% (1.00) |
| high | 35.6% (1.00) | 35.6% (2.00) |

Nearly double, from a config value. Then the controls.

**Control 1: rank by popularity alone.** Similarity still picks the candidate
pool but contributes nothing to the ordering. **32.2%** - so 13.9 of the 17.3
points came from sorting by review count and 3.4 from the embedding. Most of
the "ranking improvement" is not ranking.

**Control 2: look at the labels.** All 37 expected app_ids have >= 11,267
reviews, median 84,488, **none under 1,000**. The eval contains no long-tail
games, so recall can only rise as the weight rises. It is structurally
incapable of reporting the cost.

**The cost, measured directly** - what the 30 queries return:

| weight (log) | recall@10 | median reviews | under 1k reviews |
| --- | --- | --- | --- |
| 0.00 | 18.3% | 68 | 79% |
| 0.05 | 25.0% | 192 | 69% |
| 0.10 | 28.3% | 674 | 54% |
| 0.20 | 31.1% | 4,715 | 30% |
| 0.40 | 34.4% | 12,366 | 6% |
| 1.00 | 35.6% | 17,889 | 1% |

At the eval optimum, 1% of results have under 1,000 reviews against 79%
unweighted. That is search over the Steam top few thousand - the same trade
#24 refused, reached from the other side and wearing a better number.

**Shipped `rrf w=0.20`**: the smallest weight that fixes the observed defect
(Cities: Skylines II above a 27-review asset flip for "city builder") while
leaving 70% of results in the tail. 25.0 / 26.7 / 30.0 / 41.1 / 42.2% across the
five thresholds. Ten points below the eval optimum, deliberately.

`rrf` over `log` because they are equivalent at matched tail cost - log 0.05
= rrf 0.20 = 25.0% at ~70%, log 0.20 = rrf 1.00 = 31.1% - so the tiebreak is
durability: log's weight is calibrated against the model's cosine spread
(qwen3 0.752-0.696, arctic 0.577-0.502), rrf reads only ranks and survives a
model swap. The model choice is still provisional (#25), so that is worth
having.

**What to do about it.** `queries.yaml` needs labelled queries whose answers are
obscure - "cozy game about running a bookshop" rather than "city builder". Until
then the harness cannot tell a better ranker from a more popular one, and any
weight above 0.20 is unjustifiable from evidence this file contains. This is the
fourth falsified hypothesis here (#19, #20, #25), and the first where the
*metric* rather than the idea was the thing that was wrong.

### 27. The fix for #26 was labelled long-tail queries. They were not long tail (2026-09-06)

#26 ended with "`queries.yaml` needs labelled queries whose answers are
obscure". I wrote 22, ran the sweep, and got the opposite of the predicted
shape: recall on the new tier **rose** with the popularity weight, 54.5% at
w=0.2 to 68.2% at w=1.0. Two independent mistakes, both mine, both in the
ground truth rather than the ranker.

**Mistake 1 - the sampler returned the head of the tail.**
`sample_longtail.sql` selected `DISTINCT ON (g.tags[1]) ... ORDER BY
g.tags[1], g.total_reviews DESC` over a 50-5,000 review band. `DISTINCT ON`
keeps the first row per group, so that is the *most*-reviewed game in each tag
- the top edge of the band. I then picked the 22 I recognised. Measured
afterwards, every target sits in the 93rd-98th percentile of the corpus by
review count, median **97.1**. Against the 55,120 games above the review
threshold: 44% have 11-49 reviews, 69% have under 200, and the median
searchable game has ~90. The tier had no games from any of that.

**Mistake 2 - the queries were paraphrases of the embedded text.** The sampler
printed 72 characters of `short_description`, which is part of `embed_text`, and
I wrote each query while reading it. That puts the target at cosine rank ~1, and
RRF's popularity term is bounded by `w/(k+1)` = `w/61`. At w=0.2 that is 0.0033,
the same as the gap between cosine rank 1 (1/61) and rank 16 (1/76) - so the
term can move a game about fifteen places and **cannot** dislodge a rank-1 hit.
The tier reported "no harm" by construction. Contamination did not merely
inflate the number; it removed the tier's ability to detect the harm it existed
to detect.

**The measurement that does work, and needs no labels.** Median review count of
everything returned, and the share under 1,000. It cannot be gamed by target
selection or by query authorship, because it does not use either. Sweep at
threshold 10, `rrf`, 52 queries:

| w | core | specific | median reviews returned | under 1k |
| --- | --- | --- | --- | --- |
| none | 18.3% | 54.5% | 65 | 79% |
| 0.05 | 18.3% | 54.5% | 69 | 77% |
| 0.10 | 18.3% | 54.5% | 82 | 75% |
| **0.20** | **25.0%** | 54.5% | **165** | **70%** |
| 0.40 | 26.7% | 59.1% | 438 | 60% |
| 1.00 | 31.1% | 68.2% | 4,021 | 23% |
| 2.00 | 35.6% | 68.2% | 9,315 | 5% |

Both recall columns climb monotonically to the end of the sweep. The plan for
this work named that shape in advance as the disqualifying one: "If it climbs
without limit, the term is measuring the ground truth's bias." It does, so it
is, and recall@10 is not the selector.

**Why the new tier rose.** At w=1.0 the median returned game has 4,021 reviews
- the 97.6th percentile, which is precisely the stratum the 22 targets occupy
(median 97.1). The ranker was not finding obscure games better; it was
returning games at exactly my targets' popularity level.

**`rrf w=0.20` stands, on a different argument than #26 gave it.** Core recall
gained per point of under-1k share surrendered:

| step | core gain | tail cost | ratio |
| --- | --- | --- | --- |
| none -> 0.20 | +6.7 | 9 pts | **0.74** |
| 0.20 -> 0.40 | +1.7 | 10 pts | 0.17 |
| 0.40 -> 1.00 | +4.4 | 37 pts | 0.12 |
| 1.00 -> 2.00 | +4.5 | 18 pts | 0.25 |

0.20 is four times more efficient than any step above it, and it is a corner
rather than a preference. #26 called it "deliberately ten points below the eval
optimum", i.e. chosen by caution; it is also the knee of the curve. The ratios
above 0.20 are upper bounds, because those core points are partly the ground
truth's bias being paid back to itself.

**Still open.** There is no genuine long-tail tier. `sample_longtail.sql` is
rewritten to sample 30-300 reviews ordered by `md5(app_id::text)` and to print a
percentile column, so the next attempt is checkable at a glance - but the
contamination problem has no clean answer, because writing a query about a game
with 80 reviews means reading something about it, and everything available is in
`embed_text`. Best available discipline is in the file header: read the blurb,
then write the query in a player's words rather than the store page's. Until
that exists, the tail-cost columns are the only honest evidence about ranking
weight, and this file has no basis for a weight above 0.20.

Fifth falsified hypothesis (#19, #20, #25, #26), and the second running where
the metric rather than the idea was wrong. #26 caught the ground truth being
biased; #27 is the fix for that bias having the same bias.

### 28. A long-tail tier that works, and the half of the fix that did nothing (2026-09-06)

#27 ended with "there is no genuine long-tail tier" and named two mistakes to
fix. Both were fixed. Only one of them mattered, and it was not the one the
write-up spent most of its words on.

**The tier.** 22 new queries, `tier: tail`, targets sampled by the rewritten
`sample_longtail.sql` at 30-300 reviews. Where `specific`'s targets sit at the
97th percentile of the searchable corpus, these sit at the 37th-75th, median
57th. Swept over the same grid as #27:

| w | core (30) | specific (22) | **tail (22)** | median revs | under 1k |
| --- | --- | --- | --- | --- | --- |
| none | 18.3% | 54.5% | **63.6%** | 65 | 81% |
| 0.05 | 18.3% | 54.5% | **63.6%** | 70 | 80% |
| 0.10 | 18.3% | 54.5% | **63.6%** | 79 | 78% |
| 0.20 | 25.0% | 54.5% | **63.6%** | 138 | 73% |
| 0.40 | 26.7% | 59.1% | **59.1%** | 395 | 63% |
| 1.00 | 31.1% | 68.2% | **36.4%** | 3,306 | 28% |
| 2.00 | 35.6% | 68.2% | **9.1%** | 8,219 | 8% |

This is the shape the ranking plan named in advance and #26 asked for: flat,
then a peak, then collapse. Overall recall peaks at w=0.40 and falls, instead of
climbing to the end of the sweep. `specific` rises 54.5 -> 68.2 across the same
sweep that takes `tail` from 63.6 to 9.1 - same query style, same
one-right-answer construction, same author, same afternoon. The only variable
that differs between the two tiers is target popularity, which makes this a
controlled result rather than another guess.

**The weight, on direct evidence at last.** w=0.20 is the largest weight that
costs the tail literally nothing: 63.6% at w=none and 63.6% at w=0.20, while
core gains 6.7 points. w=0.40 buys 1.7 more core points for 4.5 tail points, and
it is downhill from there. #27 justified 0.20 as the knee of a marginal-cost
curve computed against an unlabelled proxy; it is now the last setting before
measured harm begins. Same number, third and best reason.

**The half that did nothing.** #27 blamed two things: a sampler that ordered
`DISTINCT ON` groups by `total_reviews DESC`, and queries written while reading
`short_description`, which is inside `embed_text`. The fix for the second was to
write "in the words a player would use six months after finishing the game."
Measured afterwards rather than assumed:

- Content-word overlap with the target's own `embed_text`: **37% mean in both
  tiers.** Identical. The German queries score 0% only because `embed_text` is
  English, so the English tail queries are in fact worse than the average says.
- Pure-cosine rank of the target: **9 of 22 tail targets at rank 1**, against
  `specific`'s 7 of 22. Median rank 4.0 against 5.5. By the metric #27 used to
  explain why `specific` could not price a weight, the new tier is *more*
  contaminated.

The tier still works, so the explanation in #27 was incomplete. Cosine rank was
never the whole mechanism. RRF scores `1/(k+r_cos) + w/(k+r_pop)`, and the
`specific` targets are at rank 1 on cosine *and* near the top on reviews, so the
weight pays them twice; a tail target at cosine rank 1 with 60 reviews is at the
bottom of `r_pop`, and the identical term pushes it down. **Target obscurity
prices the weight. Query prose does not.** The discipline that matters lives in
the sampler, not in the writing - which is the opposite of where the effort went.

Worth keeping for its own sake: the fix had two parts, one plausible and one
mechanical, and they were only separable because each was measured on its own.
Shipped together and called a success, the prose rule would have been recorded
as the thing that worked and repeated on the next tier.

**Not fixed, deliberately.** Four tail targets are outside the top 200 under
pure cosine, so they score zero at every weight and carry no information about
ranking. Dropping them would raise the tier's headline from 63.6% and would be
exactly the target-selection bias this entry is about, so they stay. Absolute
recall in this tier is not a quality number; only its slope is evidence.

Also worth stating: the counter-metric moved when the query set grew (median
returned at w=0.20 was 165 over 52 queries and is 138 over 74). It is comparable
only within a fixed query set - it prices ranking configs against each other,
never one query set against another.

Sixth falsified hypothesis (#19, #20, #25, #26, #27), and the first one where
the headline fix succeeded while the reasoning behind half of it was wrong.

### 29. The eval doubled, and two of #28's claims did not survive it (2026-09-06)

The arctic-vs-qwen3 comparison came back split - arctic ahead on `specific` by
13.7 points, behind on `core` by 7.2, level on `tail`, German and the
counter-metric. At n=22 per tier one query is 4.5 points, so the entire verdict
rested on two or three queries. Rather than ship a model on that, the eval grew:
**118 queries now - `core` 30, `specific` 44, `tail` 44, German 27.** Targets
verified present, embedded and above threshold before any query was written.

**The good news first: #28's shape reproduced out of sample.** The second 22
tail queries were written after that entry, against a different embedding model,
from tags the first batch had not used. They behave the same way:

| w | tail (22, qwen3) | tail (44, arctic) | specific (44) | under 1k |
| --- | --- | --- | --- | --- |
| none | 63.6% | 75.0% | 75.0% | 84% |
| 0.10 | 63.6% | 75.0% | 79.5% | 80% |
| 0.20 | 63.6% | 72.7% | 79.5% | 75% |
| 0.40 | 59.1% | 70.5% | 86.4% | 64% |
| 1.00 | 36.4% | 47.7% | 88.6% | 35% |
| 2.00 | 9.1% | 6.8% | 88.6% | 11% |

Flat, then falling, while `specific` climbs monotonically to 88.6%. A finding
that survives new queries and a new model is worth more than the original
measurement was.

**Claim that was overstated: "the only variable that differs is target
popularity."** #28 said the `specific`/`tail` contrast was controlled. It was
not, quite: `tail` came from `sample_longtail.sql` and `specific` was
hand-picked, so sampling method varied alongside popularity. `sample_specific.sql`
now mirrors the tail sampler exactly - same `DISTINCT ON`, same `md5(app_id)`
ordering, same percentile column, only the review band differs (5,000-200,000
against 30-300). The 22 new `specific` targets came from it and land at an
average 97.3rd percentile against the hand-picked batch's 97.1, so the claim is
now true rather than merely plausible. It was written as though it were already
true, which is the actual mistake.

**Claim that was wrong: the EN/DE split measures language.** German recall moved
from 31.0% to 42.6% purely from adding six queries, which is not how a language
property behaves. The two sets do not have the same tier mix - German is 37%
`core` queries against English's 22%, and `core` scores about a quarter of what
the other tiers do. Per tier, on arctic at w=0.20:

| tier | EN | DE | gap |
| --- | --- | --- | --- |
| core | 19.2% (20) | 15.0% (10) | 4.2 |
| specific | 85.7% (35) | 55.6% (9) | 30.2 |
| tail | 75.0% (36) | 62.5% (8) | 12.5 |
| aggregate | 66.8% (91) | 42.6% (27) | 24.3 |

Same data, four different answers. "German is ~20 points behind" was a statement
about the query mix as much as about German. `run_eval` prints this matrix now,
with the aggregate rows kept but labelled as not a language measurement. The
cells are small - 8 to 10 German queries each, so one query is 10-12 points -
and a single row is a hint, not a result.

**Still unresolved: which model ships.** Expanding the eval invalidated the qwen3
baseline, because a tier's composition changed and recall is not comparable
across query sets - the same reason the counter-metric is only comparable within
a fixed set (#28). Settling it needs qwen3 re-embedded and run on all 118. That
cost was accepted deliberately: the alternative was picking a model on three
queries.

**Noted for whenever the weight is revisited:** arctic's largest
no-tail-cost weight is 0.10, not qwen3's 0.20 - it loses 2.3 tail points at 0.20
on 44 queries, and lost 4.6 on 22. The optimal weight is model-dependent even
under RRF, which is rank-based and was adopted partly because it was expected not
to be. Acting on that is a search-ranking change and belongs in plan mode.

### 30. Arctic wins, and #25's crossover story was an artifact of its instrument (2026-09-06)

#29 left the model choice open because the eval was too small to settle it.
Doubled to 118 queries and run on both models, same grid, same index settings,
same day, it settles:

| @ rrf w=0.20 | qwen3 | arctic | delta |
| --- | --- | --- | --- |
| core (30) | **25.0%** | 17.8% | -7.2 |
| specific (44) | 63.6% | **79.5%** | **+15.9** |
| tail (44) | 63.6% | **72.7%** | **+9.1** |
| EN (91) | 57.1% | **66.8%** | +9.7 |
| DE (27) | 42.6% | 42.6% | 0.0 |
| overall | 53.8% | **61.3%** | **+7.5** |
| median revs / under 1k | 132 / 74% | 137 / 75% | - |

One query is 0.85 points at n=118, so +7.5 overall is about nine queries and
+15.9 on `specific` is seven - against the two or three that the 74-query set had
been about to decide it on. It holds at `w=none` too (58.1 against 50.4), so it
is retrieval quality rather than an interaction with the popularity term, and
the counter-metric is unchanged, so arctic is not buying recall by deleting the
tail. **Ship arctic.**

**#25's crossover was an artifact.** That entry measured the three models while
raising `REVIEW_THRESHOLD` and concluded "qwen3 wins below ~1,000 reviews, arctic
wins above it by 19 points," shipping qwen3 because threshold 10 is what ships.
Raising the threshold *deletes rows from the corpus*, which is not the same
experiment as *asking for an obscure game*. The `tail` tier asks directly - 44
queries whose right answer has 30-300 reviews - and arctic wins it by 9.1 points
at w=0.20 and 11.4 at w=none. There was never a regime where qwen3 was better at
finding obscure games; there was a regime where the corpus had been cut down to
1,702 rows and the two models were being scored on 30 queries about famous ones.
The instrument, not the model, produced the crossover.

Worth stating because #25 was careful, reported the whole curve rather than one
number, reproduced every figure in a second pass, and was still wrong about what
the curve meant. Running more conditions does not help when all of them are the
wrong measurement; only a different measurement does.

**Arctic's gain is English-only, on the tier where it is largest.** Per tier and
language at w=0.20:

| tier | qwen3 EN / DE | arctic EN / DE |
| --- | --- | --- |
| core | 25.0 / 25.0 | 19.2 / 15.0 |
| specific | 65.7 / 55.6 | 85.7 / 55.6 |
| tail | 66.7 / 50.0 | 75.0 / 62.5 |

`specific` English goes 65.7 -> 85.7 while German sits at 55.6 for both models -
the same 5 of 9. n=9 is small enough that the exact tie is luck, but the shape is
not: swapping to the model whose selling point is multilingual retrieval bought
20 points of English and nothing German. German remains the weak spot and is not
a model problem. That points at `embed_text`, which is English, rather than at
the encoder - the next thing worth trying is a German-language document field or
query translation, not a fourth model.

**qwen3 wins `core` alone**, by 7.2 points, and that is the tier CLAUDE.md
already documents as understating quality: arctic returns Cities: Skylines II and
Roguebook for "city builder" and "deckbuilding roguelike", both correct, both
unlisted, both scoring zero. It also returns a 44-review 50%-positive "Megacity
Builder", which is a real defect. Both things are true and `core` cannot separate
them, which is why it does not decide this.

**Open, and needing plan mode:** arctic's largest no-tail-cost weight is 0.10,
not 0.20 - `tail` is 75.0% at w<=0.10 and 72.7% at 0.20. By the rule in
CLAUDE.md, which picks the weight from `tail` and the counter-metric, arctic
should ship at 0.10. That is a search-ranking change and does not belong in this
commit.
