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
| tail (44) | 63.6% | **70.5%** | **+6.9** |
| EN (91) | 57.1% | **65.8%** | +8.7 |
| DE (27) | 42.6% | 42.6% | 0.0 |
| overall | 53.8% | **60.5%** | **+6.7** |
| median revs / under 1k | 132 / 74% | 147 / 74% | - |

CORRECTED. The arctic column originally read tail 72.7 / EN 66.8 / overall 61.3,
measured on a corpus that had been embedded in two halves under different
`num_batch` settings. The figures above are from the uniform corpus that ships.
The verdict does not change; see #31 for why the numbers moved at all.

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
queries whose right answer has 30-300 reviews - and arctic wins it by 6.9 points
at w=0.20 and 9.1 at w=none. There was never a regime where qwen3 was better at
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
| tail | 66.7 / 50.0 | 72.2 / 62.5 |

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

**The weight question this raised is settled in #31, and the answer is no
change.** The apparent case for dropping to 0.10 was a 2.3-point tail
difference, which turns out to be exactly the size of this eval's
reproducibility floor.

### 31. The eval has a reproducibility floor, and it is one query wide (2026-09-06)

#30 left one thing open: arctic's largest no-tail-cost weight looked like 0.10
rather than the shipped 0.20, so the weight seemed to need changing. Before
proposing that, a finer sweep - 0.10 / 0.125 / 0.15 / 0.175 / 0.20 / 0.25 - to
find where `tail` actually falls instead of snapping to a grid point.

It disagreed with the earlier run. Same model, same 118 queries, same config,
`tail` 2.3 points lower at every weight, while `core` and `specific` came back
identical. That is one query out of 44.

**Isolating it, cheapest experiment first:**

| test | result | rules out |
| --- | --- | --- |
| same config twice, same corpus | byte identical | query-time nondeterminism |
| drop and rebuild HNSW, same vectors | byte identical | index graph build order |
| re-embed the same model | `tail` moves 2.3 points | leaves the vectors |

So the vectors themselves differ between two embeds of the same model on the
same corpus, and there is a specific reason rather than general GPU noise: the
first arctic corpus was embedded in two halves under **different `num_batch`
settings** - 27,648 rows at Ollama's default 2,048 before that setting existed
(#28's crash and resume), then 103,003 at 4,096 after. Physical batch size
changes how the forward pass is grouped, which changes floating-point summation
order, which flips results that sit near a tie. The corpus that ships was
embedded uniformly at 4,096.

**Consequences, in order of importance.**

1. **The weight stays at 0.20.** The whole case for 0.10 was `tail` 75.0% vs
   72.7%, a 2.3-point difference. The reproducibility floor is also 2.3 points.
   There is no evidence here, and adopting 0.10 would have been fitting to noise
   with a ranking change to show for it. `tail` on the uniform corpus is 72.7%
   at w=none and 70.5% from 0.125 up, so the cost of the shipped weight is one
   query either way.
2. **The model verdict is unaffected.** Arctic beats qwen3 by 6.7 points overall
   and 15.9 on `specific` - eight and seven queries. Comfortably above a
   one-query floor, which is the only reason #30 survives this entry intact.
3. **Differences under ~2.5 points at n=44, or ~1 point at n=118, are not
   results.** That threshold now has a measurement behind it rather than an
   intuition, and it retires a habit: three of this week's entries reported
   differences in that range as though they meant something.
4. **Never change embedding batch settings mid-corpus.** It produces a corpus
   embedded two different ways, and nothing downstream can detect it -
   `verify_corpus_model()` sees one model name and `verify_corpus_complete()`
   sees no gaps. Both guards pass on a corpus that is quietly inhomogeneous.

**What this cost and what it bought.** Four extra evals and an index rebuild,
about twenty minutes, to decide *not* to make a change. That is the cheapest
outcome available: the alternative was a plan-mode ranking change justified by a
number I had not checked was real. The finer sweep was run to locate a cliff
precisely and instead showed the cliff was inside the noise - which is the same
lesson as #26 through #30, arriving for the fifth time. The measurement keeps
being the thing that needs measuring.

---

### 32. "single player" was parsed away, and the reference tags argued with the filters (2026-09-06)

**Reported.** `call of duty like game, but not including itself, also popular,
single player` returned **Counter-Strike** at rank 1. Two independent defects,
both in the parser path. Neither was in the ranking, which is where I would have
looked first if the `filters:` line had not printed the answer.

**Defect 1: the filter was never applied.** The CLI printed `filters: >= 1,000
reviews  not Call of Duty®` and nothing else. The model returned
`multiplayer=None`, so `_apply_filters` added no `NOT EXISTS` clause and every
multiplayer shooter in the corpus stayed eligible.

It is not a general singleplayer failure. Measured at temperature 0, so these
are deterministic, not samples:

| query | `multiplayer` |
| --- | --- |
| `single player shooter` / `solo shooter` / `shooter to play alone` | `False` |
| `call of duty like game, single player` | `False` |
| `call of duty like game, under 20 dollars, single player` | `False` |
| `call of duty like game, on linux, single player` | `False` |
| `... but not including itself, single player` | `False` |
| `... also popular, single player` | `False` |
| **`... but not including itself, also popular, single player`** | **`None`** |
| `... single player, but not including itself, also popular` | `False` |

Every clause individually is fine. All four together, with "single player" last,
is not - and moving it earlier in the *same* sentence brings the filter back.
This is #13 and #22 for the third and fourth time: **the prompt is full**, and
what falls off is whatever the query mentions last.

**Defect 2: `apply_reference` borrowed tags that contradict the filters.** It
appends the referenced game's top 6 tags to `semantic_query` and did so with no
regard for what had just been extracted:

| query | the WHERE clause | the text being embedded |
| --- | --- | --- |
| `like stardew valley but not multiplayer` | `multiplayer=False` | `Multiplayer` |
| `like resident evil but nothing scary` | exclude `Horror` | `Horror, Survival Horror` |
| `like elden ring but not difficult` | exclude `Difficult` | `Difficult` |
| the reported query | (should be) `multiplayer=False` | `Multiplayer` |

So SQL deleted a category while the vector hunted for it. The reference game's
tags describe *the game*; they are not the *request*, and nothing was checking
the difference.

**Isolating them.** Hand-built `ParsedQuery`, so the parser is out of the loop:

```
A  as shipped            1. Counter-Strike  4. Enlisted  5. WARMODE  6. Arma 3  8. Verdun
B  + multiplayer=False   1. Ravenfield  2. Call of Juarez: Gunslinger  8. System Shock
C  + drop borrowed tag   1. Ravenfield  2. HOLE  4. Deadlink  6. SUPERHOT
```

Defect 1 is the whole reported bug - B is what evicts every multiplayer title.
Defect 2 only reshuffles ranks. Worth fixing because it is indefensible, not
because it was expensive.

**Fix.** Both in code, and the prompt was not touched:

- `wants_singleplayer()` in `query_parser.py`, beside `wants_popular()`. EN and
  DE, negation-guarded so "not single player" and "kein Einzelspieler" do not
  invert the filter - a missing filter returns too much, a backwards one returns
  confidently wrong results. It **fills only, never overrides**: the model was
  observed returning no value, never a wrong one, and an override would break a
  mixed ask like "single player or co-op" that the model reads correctly. No
  `wants_multiplayer()` - "no multiplayer" contains "multiplayer", so the `True`
  direction is the riskier half for a failure never observed.
- `_contradicts_filters()` in `title_lookup.py`, filtering `borrowed` against
  `excluded_tags` and the multiplayer axis before it is appended.

**What this does NOT fix.** Two things, stated rather than papered over:

1. `_contradicts_filters` is **exact match**. Excluding `Horror` still borrows
   `Survival Horror`; excluding `Fantasy` still borrows `Dark Fantasy`. A
   substring rule would catch those and would also make an excluded `Action`
   drop `Action RPG` and `Action Roguelike`, which is a much larger behaviour
   change than this bug justifies.
2. **The prompt is still full.** This is a net under the defect, not a repair.
   Every field in `ParsedQuery` is exposed the same way, and the next one added
   will be exposed again. The durable fix is splitting the parse into two calls,
   which is a change worth its own measurement.

**The part that should have caught this.** Nothing did, because nothing could:
there was no singleplayer or solo case anywhere in `compare_parsers.py` **or**
`queries.yaml`. The reported query is now in `compare_parsers.py` alongside the
Resident Evil contradiction, for the same reason the four-constraint query was
added after a prompt edit destroyed platform extraction - a harness cannot catch
a regression it never exercises.

`queries.yaml` was deliberately **not** extended. Adding queries invalidates
every recall number measured on the current set (see #29), and this defect is
not a recall problem: `run_eval`'s shipped numbers come from the path *without*
`--parse`, so they cannot see a parser change at all.

**And the regression check found something else.** `run_eval --parse` before and
after: 53.7% -> 54.1% overall, below the ~1-point floor at n=118. But `specific`
and `tail` were *identical* and exactly one query moved - `Aufbauspiel mit
Automatisierung`, 0% -> 50%.

That query cannot be touched by either fix, and the gates are independent:
`wants_singleplayer()` returns False on it, its `reference_game` is None so
`apply_reference` returns before the new filter runs at all, and its
`excluded_tags` are empty. So the difference came from somewhere else - and it
did. Parsed five times in a row it gives `['Base-Building', 'Automation']` every
time, and searched three times with those tags it scores 50% every time, finding
Factorio. The baseline run scored 0%, so **the baseline parsed different tags**.

**The parser is deterministic within a run and not across runs**, at
`temperature=0`, on the same model and the same query. #31 measured a
reproducibility floor for the embedding path and found the vectors were the
non-deterministic part; this is the same lesson one layer up. So
`run_eval --parse` has a floor of its own, it is at least one query wide, and a
`--parse` difference of one query is not evidence of anything. Worth knowing
before someone reads a 0.4-point parser "improvement" as a result - which is
exactly what this entry would have said if I had not checked which query moved.

---

### 33. Every field was optional, so the model just stopped emitting them (2026-09-07)

**Reported.** `game that feels like call of duty, but no wars on linux under 30$`
extracted `linux` and `not War` but no price. The suspicion was the `$` sign
rather than the word "dollars".

**That was wrong, and cheap to kill.** Each notation alone parses fine - `30$`,
`$30`, `30 dollars`, `30 USD`, `30 bucks`, "cheaper than 30$" all give 30. And
inside the full query *all four* notations fail identically. So it is not the
symbol, and it is not the wording.

**Root cause: `_llm_schema()` marked every field optional but `semantic_query`.**
Pydantic puts a field in `required` only when it has no default, and every field
except `semantic_query` carries one. Ollama's `format` compiles that schema into
a grammar, and an optional property is a branch the model may simply skip. Raw
output for the failing query:

```json
{ "semantic_query": "game that feels like call of duty",
  "excluded_tags": ["War"], "platforms": ["linux"], "max_required_age": null }
```

`max_price_usd` is **absent**, not null. Downstream that is identical to "no
price requested", so the filter vanished with no error and no log line. Note the
model understood perfectly well - it stripped "under 30$" out of
`semantic_query`. It just never emitted the key.

**Two things this had been hiding.**

1. **The field-order invariant was already broken.** CLAUDE.md says order is
   load-bearing and `semantic_query` must be declared last because generation
   follows declaration order. It does not: with optional properties the model
   emits keys in whatever order it likes, and it put `semantic_query` FIRST -
   the exact failure declaring it last was meant to prevent. Setting `required`
   restores real declaration order, verified against the raw keys.
2. **It was systemic, not a price quirk.** Over the `compare_parsers` set x 3
   reps the shipped schema dropped `required_tags` on **64%** of parses (27/42)
   against 7% when required - including `co-op base builder under 20 dollars
   that runs on linux`, which is the prompt's own worked example.

**Then the obvious fix made recall worse, and that was the interesting part.**

| schema | overall | core | specific | tail |
| --- | --- | --- | --- | --- |
| shipped | 54.1% | 12.8% | 72.7% | 63.6% |
| scalars required | 51.6% | 12.8% | 70.5% | 59.1% |
| every field required | 46.5% | 12.8% | 68.2% | 47.7% |

Forcing the fields makes the model emit `required_tags` far more often - on the
`tail` tier, 23/40 queries to 37/40, mean 0.70 to 1.18 tags - and every extra tag
was another `@>` conjunct.

**The second defect, which the first one was masking.** `required_tags` was
ANDed: `Game.tags.contains()`, `tags @> ARRAY[...]`. One parse per query, four
filter semantics, n=118:

| tag filter | overall | core | specific | tail | under-delivered |
| --- | --- | --- | --- | --- | --- |
| `@>` all-of | 55.4% | 11.1% | 72.7% | 68.2% | 6 |
| `&&` any-of | 60.0% | 16.1% | 79.5% | 70.5% | 0 |
| first tag only | 57.9% | 14.4% | 77.3% | 68.2% | 0 |
| no tag filter at all | 61.3% | 17.8% | 79.5% | 72.7% | 0 |

Of the 72 queries that got tags, ANDing **helped 3 and hurt 11** - and all three
it helped carried exactly one tag. `running a bookshop and taking on cosmic
horror` returned **zero rows**: nothing in 130,651 games carries `Cozy` AND
`Horror` AND `Investigation`. With `&&` that is 8,544 games.

**The instrument could not referee this.** Only 7 of the 118 eval queries contain
anything constraint-like, and **none** names a price, platform, year or age. So
every filter the parser extracts can only shrink the candidate set, and
`run_eval --parse` measures how little the parser does rather than how well it
parses. That is why "delete the tag filter" tops the table above and is still the
wrong answer - and why the middle table is not evidence against requiring the
scalars. Proof it is blind rather than merely unkind: strip the tag arrays out of
the schema entirely and `--parse` scores 60.5 / 17.8 / 79.5 / 70.5, which is the
no-parse baseline to the decimal. With no tags the parse is a no-op on this set.

**A third defect, created by the first fix.** Making `platforms` required means
the model must emit the key - and on a query naming no OS it sometimes fills all
three rather than an empty list. `cheap relaxing puzzle games, nothing scary`
did it 3 times out of 3. Platforms are ANDed in `_apply_filters`, so that
silently demands a game running on Windows AND macOS AND Linux. Measured at 1 of
12 no-OS queries in `compare_parsers` and 0 of 40 eval queries, so it is narrow
but deterministic where it fires.

**Later correction.** Building the parser eval, I disabled the guard to prove the
harness would catch this - and the invention would not reproduce at all, 0 of 4
on the same query that had given 3 of 3. So "3/3 deterministic" was true within
one run and not across sessions, which is #33's own lesson applied to #33. The
guard stays (the bug was real when measured, and it can only widen results), but
the harness's ability to catch it is UNPROVEN rather than demonstrated.

The first check for this missed it: I counted "invented scalars" over
`max_price_usd`, `min_price_usd`, `released_after`, `multiplayer` and
`max_required_age` - which came back 0 of 21 - and never looked at `platforms`,
the one array in the required set. A negative result is only as wide as the
fields you actually looked at.

Leaving `platforms` optional instead is worse, and measured: the key then goes
missing on `on linux under 30$` and the real platform filter disappears, which is
the original bug. So it is guarded in code -
`_drop_invented_platforms()` drops a three-platform list when the query names no
OS at all. A genuine "runs on windows, mac and linux" survives, and the guard can
only ever widen results.

**Fix, all three parts, because either of the first two alone is a regression.**

- `_REQUIRED_FIELDS` in `query_parser.py`: the scalars plus `platforms` and
  `semantic_query`. The tag ARRAYS stay optional - forcing those is the 47.7%
  tail row.
- `Game.tags.contains()` becomes `Game.tags.overlap()` in `_apply_filters`. One
  GIN index serves both operators, so no migration; confirmed by `EXPLAIN` that
  `&&` still takes a bitmap scan on `ix_games_tags_gin`.
- `_drop_invented_platforms()` in `query_parser.py`, for the defect above.

**The result, and it took four runs to state honestly.** Against a
baseline-equivalent run in the same session (both changes undone by monkeypatch,
so the corpus and the Ollama state match), n=118:

| | baseline | with both | delta |
| --- | --- | --- | --- |
| overall | 55.8% | 60.0% | +4.2 |
| `specific` | 72.7% | 84.1% | +11.4 |
| `core` | 12.8% | 16.1% | +3.3 |
| `tail` | 68.2% | 65.9% | **-2.3** |

So it is **not** better on every tier. `specific` carries the whole gain;
`core`'s +3.3 is one query of 30; and `tail` is DOWN 2.3, which is exactly the
+-2.5 floor at n=44 - forcing the scalars makes the model emit more tags, and
obscure games are the least likely to carry them. Counter-metric flat: 159 median
reviews returned and 73% under 1k, against 143 and 74%.

The first draft of this entry claimed "+5.9 and better on every tier", from one
run against an older baseline pair. Four runs of the new code gave 56.6, 59.2,
60.0, 60.0 and two baseline-equivalents gave 55.8 twice - so the honest claim is
+4.2, and the spread on the new code is wider than the gap on `tail`.

**And that spread is itself a finding.** #32 put the `--parse` reproducibility
floor at "at least one query wide". It is wider: four queries moved between two
runs of identical code - `playing as a mouse sneaking through a ruined castle`,
`Rätselspiel...`, `hunting monsters and cooking...` and `Puzzlespiel, das
Schachzüge...`, all 0% then all 100%. Chasing them cost two wrong diagnoses:
first "the platform guard fixed them" (it did not - all four parse with
`platforms: []`), then "the model over-strips `Rätselspiel`" (it does, 3/3, but
that query scores 100% anyway). Both were stories told about noise. A `--parse`
difference under ~3.5 points is not a result, and a per-query flip is not a
mechanism until the same code reproduces it.

**What it costs.** Parse goes 0.56s to 1.06s. Purely output tokens - 46-52 to
91-99 at a constant ~75 tok/s - and unavoidable if the model must emit every key.
The API's first-search path goes ~1.35s to ~1.85s; the chip path still does not
re-parse and stays at 0.095s.

**What to take from this.** Three things worth more than the bug.

1. **An optional field in a constrained-decoding schema is an invitation to
   omit it.** `format` guarantees the output *validates*; it does not guarantee
   the output is *complete*, and a schema built from Pydantic defaults is almost
   entirely optional by accident. Absence then reads downstream as "not
   requested".
2. **"The prompt is full" was over-applied.** #13, #22 and #32 all blamed prompt
   saturation for a vanishing filter. Some of that stands, but this mechanism
   explains the same symptom and is a one-line schema property. Check what the
   grammar permits before rewriting a prompt.
3. **An eval made of pure descriptions cannot price constraint extraction.** It
   can only punish it. Before reading a `--parse` number as a verdict on the
   parser, check whether any query in the set contains the thing being parsed.

---

### 34. The constraint became a filter and stayed in the query vector anyway (2026-09-07)

**Found while answering a different question.** "I need a game on which we play
as a cat exploring city or ruins, which is also popular" did not return Stray.
Stray is not the bug - see the bottom of this entry - but the parse was:

```
required_tags: ['Cats']   min_reviews: 1000
semantic_query: 'cat exploring city or ruins popular'
```

`wants_popular()` had correctly converted the intent into a 1,000-review floor
and then left the word `popular` sitting in `semantic_query`, where it gets
embedded and compared against game descriptions. It says nothing about what a
game IS, so it can only match noise.

**It leaked on 8 of the 9 queries that asked for it** - `popular`, `famous`,
`well-known`, `best-selling`, German `bekannte`. The one that escaped only did so
because the model happened to rewrite that sentence anyway.

This is #32's lesson in a new place. There it was `apply_reference` appending
tags that contradicted the filters just extracted; here it is a word that has
already become SQL still steering the vector. **A constraint that has been
converted into a filter must stop influencing the embedding**, and nothing in
the codebase was enforcing that as a rule rather than case by case.

**A second bug in the same regex, found by testing the German side.**
`_POPULAR` listed `bekannt\w*` with a suffix wildcard but `beliebt` bare. Nobody
writes the uninflected adjective - "beliebte Aufbauspiele" is the ordinary form -
so `beliebte`, `beliebten`, `beliebtesten` and `Beliebtheit` **fired nothing at
all**. German queries asking for popular games silently got no review floor. The
fix is one wildcard; `beliebig` ("arbitrary") is safely excluded because it
diverges before the `t`, which was checked rather than assumed.

**Fix.** `_strip_popular()` beside `wants_popular()`, reusing `_POPULAR` itself so
the trigger and the removal cannot drift apart, called from the same branch that
sets `min_reviews`. Two guards worth keeping:

- `parse_query`'s existing empty-`semantic_query` check runs BEFORE
  `_apply_code_rules`, so it cannot catch this. A query of literally "popular"
  strips to nothing, and embedding "" is worse than embedding a useless word - so
  the strip is skipped when nothing would be left.
- It runs before `apply_reference`, which appends the referenced game's tags to
  `semantic_query`. Stripping afterwards would run the regex across the borrowed
  tag list.

**What it is worth, stated honestly.** Stray moves from cosine rank 20 to 17 and
is still not in the top 10. This is not a fix for the reported symptom and should
not be read as one: Stray loses because RRF at `w=0.20` cannot lift a rank-20
result past rank-1 cosine matches, however popular it is - `1/80 + 0.20/61`
against `1/61 + 0.20/62`. It surfaces at `w=0.40` (8th) and `w=1.00` (2nd), and
CLAUDE.md already records what those weights cost the long tail. The real answer
there is the prominence term the README names as the top open weakness.

So this change is justified by correctness, not by a number: a filter leaking
into the query vector is wrong regardless of whether any labelled query notices.
The eval cannot notice - no query in `queries.yaml` contains a popularity word
(all 9 grep hits are comments) - which is the same blindness #33 documented.

**Known limitation, not papered over.** The words can be content rather than
constraint: "play as a famous detective" now loses "famous". That reading was
already wrong before the change - `wants_popular` fired and set `min_reviews`
regardless - so this makes an existing misreading slightly worse rather than
introducing a new one. The prompt cannot arbitrate it; per CLAUDE.md it is full.
On the fallback path (model failure, `semantic_query` = raw text) the strip can
also leave a fragment like "which is also", since it removes words and
punctuation but not filler.

**Same class, still open, and the claim I first made about it was WRONG.**
This entry originally said `wants_reference_excluded` leaks identically and is
"arguably worse". Measured across 8 exclusion phrasings, the excluder word
survives in **2**, not 8 of 9 - and where it survives, what stays in the text is
a real game name, which is semantically rich and points at the neighbourhood the
user actually wants. Stripping it changed the results but not visibly for the
better. `popular` is noise about games; `call of duty` is not. Corrected in #35,
which is where measuring this properly led somewhere much more useful.

---

### 35. A one-word game title could never be recognised (2026-09-07)

**Found by trying to confirm #34's last paragraph, which turned out to be wrong.**
Checking whether `wants_reference_excluded` leaks the way `wants_popular` did, I
ran eight exclusion phrasings. The leak was minor - 2 of 8, not 8 of 9 - but four
of the eight excluded **nothing at all**:

```
open world rpg without skyrim               NO REFERENCE FOUND
roguelikes similar to hades, except hades   NO REFERENCE FOUND
racing games like forza but not forza       NO REFERENCE FOUND
```

Not the excluder logic, which works whenever a reference is found - Call of Duty
and Dark Souls both exclude correctly. `_find_referenced_game` never found the
game.

**Mechanism.** `_word_ngrams` builds 2-to-5 word windows, and its docstring says
"one of them is the game's name, or its opening". For a ONE-WORD name that is
false: `Hades` has 279,741 reviews and no two-word window from "roguelikes
similar to hades" prefixes it. `MIN_NAME_LENGTH = 6` blocked it twice over, since
"hades", "stray" and "forza" are five characters.

So this was never really an exclusion bug. It silently broke tag borrowing for
every single-word title and for anyone typing just the franchise word - `like
hades`, `like stray`, `like terraria`, `like factorio`, `like forza` all borrowed
nothing, which defeats the entire reason `title_lookup` exists (#20: the
embedding cannot get from a name to `Souls-like`).

**The obvious fix is much worse than the bug, and this is the number that
mattered.** Generating single-word candidates, scored against the 118 eval
queries - none of which deliberately names a game, so every hit is a false
positive:

| config | titles found | false positives |
| --- | --- | --- |
| shipped (2-5 word windows, min_len 6) | 0/7 | 4/118 |
| every single word, min_len 6 | 3/7 | 18/118 |
| every single word, min_len 5 | 6/7 | **24/118** |

A wrong reference appends six wrong tags to `semantic_query`, so that is a fifth
of ordinary queries actively corrupted:

```
cozy farming sim with fishing          -> Farming Simulator 22
first person puzzle game with portals  -> Persona 5 Royal   ("person" prefixes "Persona")
chaotic co-op cooking party game       -> Party Animals
rhythm game where you move to the beat -> To the Moon
```

**Fix: a single word counts only after a reference CUE.** "similar to hades"
names a game; "cooking party game" does not, and the difference is a word in
front. `_REFERENCE_CUE` captures the token after `like` / `similar to` /
`such as` / `excluding` / `except` / `without` / German `wie` / `ohne` and friends,
and those captures are appended to the candidate list:

| config | titles found | false positives |
| --- | --- | --- |
| shipped | 0/7 | 4/118 |
| **cued singles, min_len 5** | **6/7** | **4/118** |

Identical to the count it started at, and the same four identities - so the
titles are free. `_word_ngrams` itself is untouched, so the multi-word path
cannot regress, and cued singles are appended AFTER the longest-first windows so
`excluding call of duty` still resolves `phrase` to `call of duty` rather than
`call`. That ordering is load-bearing twice over: `phrase` is also what
`wants_reference_excluded` looks for an excluder in front of.

`MIN_NAME_LENGTH` went 6 -> 5, which loosens the multi-word path too. That was
measured across the whole function rather than argued: false positives did not
move. The docstring's cautionary titles (`Beat`, `GAME`, `Doll`) are four
characters and stay blocked, and the real guard was always
`TITLE_MATCH_MIN_REVIEWS` - `Nothing` is seven characters and cannot match at any
length, because 9,260 reviews is under the 50,000 floor.

**Still broken on purpose.** `without skyrim` finds nothing, and should: the real
name is `The Elder Scrolls V: Skyrim`, so no prefix of any query opens it. That
is the documented prefix-match limitation (#21, corrected by #23) and it wants
trigram matching, not this.

**What to take from this.** The bug was invisible because it fails silently and
in the direction of doing less - no error, no log line, just a query that quietly
does not borrow tags. It was only found by testing a DIFFERENT hypothesis that
turned out to be wrong. Two entries in a row have now been improved by measuring
the thing I was about to assert instead of asserting it (#33's four flapping
queries, #34's "arguably worse"), and both times the correction was worth more
than the original claim.

Also worth naming: the eval cannot score this at all. No query in `queries.yaml`
deliberately references a game, so the 118 serve as a false-positive corpus
rather than a recall measure. That is the third distinct thing #33's "the eval
cannot referee a parser change" applies to.

---

### 36. A cross-encoder is worth 5.6 points, and three of the four arms lied first (2026-09-07)

**Why reranking at all, decided by measurement rather than by BUILD_PLAN's
ordering.** Every labelled target's exact cosine rank over the searchable
corpus, bucketed by whether reranking could ever reach it:

```
already in top 10                            72    48.6%
rank 11-200   a reranker CAN fix             40    27.0%
rank >200     only retrieval can fix         36    24.3%

tier          hit@10  rerankable  outside
core               4          22       34
specific          33           9        2
tail              35           9        0
```

On the two tiers that can price a ranking change it is lopsided: **all 9 tail
misses and 9 of 11 specific misses are already in the pool** - retrieval found
them and the ordering buried them. That is the case for a cross-encoder, and it
also fixed the ceiling in advance: 112 of 148 targets are in the pool at all, so
75.7% overall is the most any reranker can produce here.

It also killed the hybrid-retrieval idea for now. The 34 `core` targets outside
the pool are mostly not bugs - Stardew Valley is 28,954th for "entspanntes Spiel
zum Abschalten" because thousands of games are relaxing and the label is one
opinion about which was meant.

**Result: `BAAI/bge-reranker-v2-m3`, cross-encoder rank substituted for the
cosine rank inside the SAME rrf sum, same k, same w.**

| config | overall | core | specific | tail | under 1k | rerank |
| --- | --- | --- | --- | --- | --- | --- |
| baseline `rrf w=0.20` | 60.5 | 17.8 | 79.5 | 70.5 | 74% | - |
| **bge fused w=0.20** | **66.1** | 20.0 | **88.6** | **75.0** | 73% | 264ms |
| bge pure w=0 | 65.3 | 16.7 | 86.4 | 77.3 | 81% | 263ms |

+5.6 overall, +9.1 `specific`, +4.5 `tail`, all clear of the ~1 point floor at
n=118, with the tail-cost counter-metric flat at 73% against 74%. English
`specific` reaches 97.1%.

**The finding that is not in the table, and that recall cannot see.** A
cross-encoder is WORSE at short genre labels. For "city builder", the query that
originally justified `w=0.20` at all:

| config | Cities: Skylines II |
| --- | --- |
| baseline | rank **1** |
| rerank fused | rank 37 |
| rerank pure | rank 52 |

It rewards literal topical match, so a game named `City Builder` with 78 reviews
beats the genre-defining title, and at `w=0` the 27-review `Square City Builder`
is back in the top 5 - the exact defect #26 introduced the weight to suppress.
`core` recall reports 17.8 -> 20.0, i.e. slightly BETTER, because core labels
2-3 games out of hundreds that would satisfy the query. So the popularity term
is doing more work than the 0.8-point overall difference suggests, and fused
wins on evidence recall does not contain. Read the probe, not the tier.

**Three of the four arms produced a number before they produced a valid one.**

*gte-multilingual-reranker-base scored a full, clean, plausible table that was
entirely the baseline.* It loads fine and then raises a CUDA device-side assert
on every `predict()`, so all 118 queries degraded to the SQL ordering exactly as
designed - and printed 60.5 / 17.8 / 79.5 / 70.5, byte-identical to the control,
which reads as "this model is no better" rather than "this model never ran".
Reranking took 16ms for 200 candidates, which was the only visible tell.
`verify_rerank_model()` had checked that the model LOADS. Loading is not scoring.
It now scores a probe pair and rejects constant scores as well, because `_ranks`
is a stable sort and constant scores reproduce the incoming cosine order exactly;
and `run_eval` now refuses to print a table at all if any query fell back. On CPU
the real error appears: `IndexError: index 5679222161408 is out of bounds for
dimension 0 with size 30` - the model's remote code against transformers 5.16.1.
Not a dtype problem, not fixable by config, and not worth downgrading
transformers project-wide. Excluded as incompatible, NOT as worse.

*Qwen3-Reranker-0.6B scored 8.1% overall, and that is my bug, not its quality.*
The seq-cls conversion still needs the Qwen `<Instruct>/<Query>/<Document>` chat
template; handed a bare `(query, document)` pair it emits near-zero logits. Same
three-document probe, sorted correctly by both models:

```
bge      0.8647  0.0002  0.0058     spread 0.8645
qwen3    0.5857  0.4921  0.4647     spread 0.1210
```

Right order, no conviction - which is fine on an obvious triple and useless
across 200 similar games. Recorded as NOT MEASURED. Reporting 8.1% as a model
result would have been the same error as the gte table, one layer up.

**What to take from this.** Every layer of this failed silently and in the
direction of "still works, just worse": Ollama 404s a rerank endpoint that does
not exist, TEI warns and continues on CPU, torch installs a CPU-only wheel
without complaint, gte falls back to the baseline behind a valid-looking table,
and Qwen3 returns real numbers with no information in them. Not one raised. The
guard that catches the dangerous one - a measurement contaminated by its own
fallback - did not exist until it had already produced a wrong table, which is
the third time this project has learned that a harness needs testing against a
known-bad input before its output means anything (#33, run_parse_eval, this).

---

### 37. The reranker I recorded as "not measured" was the best one (2026-09-07)

**#36 shipped `bge-reranker-v2-m3` having excluded `Qwen3-Reranker-0.6B` as NOT
MEASURED rather than worse** - it had scored 8.1% overall against a 60.5%
baseline, and the tell was that on a three-document probe it ordered correctly
with a score spread of 0.121 against bge's 0.865. Right order, no conviction.
The cause was the invocation: the seq-cls conversion still needs Qwen's chat
template, and the yes/no logit it was trained to emit lands after one exact
assistant preamble. Handed a bare `(query, document)` pair it has almost no
opinion.

`_pair_for()` in app/rerank.py now wraps a pair however the model wants it -
the reranker's version of `_MODEL_PREFIXES` in app/embedding.py. Same probe,
after:

```
                spread   before -> after
bge             0.8645   (unchanged, takes the raw pair)
qwen3           0.9874   was 0.1210
```

**It wins, and by more than bge won.**

| config | overall | core | specific | tail | DE | under 1k | rerank |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `rrf w=0.20` | 60.5 | 17.8 | 79.5 | 70.5 | 42.6 | 74% | - |
| bge fused | 66.1 | 20.0 | 88.6 | 75.0 | 40.7 | 73% | 264ms |
| **qwen3 fused** | **68.8** | **27.2** | 88.6 | **77.3** | **51.9** | 71% | 1,021ms |

+8.3 over the two-stage baseline, +2.7 over bge, and the tail-cost
counter-metric is slightly BETTER at 71% under 1k, so none of it is bought by
deleting the long tail. Reproduced identically across two runs.

**The mechanism is instruction-following, and it is visible rather than
inferred.** bge scores -0.01 on FollowIR - at chance - so it can only answer
"how related are these two texts". Qwen3 can be told what relevance means. That
predicts it should win exactly where bge was weakest, and it does. #36's own
counter-example, the query the popularity weight exists for:

| config | Cities: Skylines II for "city builder" |
| --- | --- |
| `rrf w=0.20` | rank 1 |
| bge | rank 37 |
| **qwen3** | **rank 1** |

bge demotes it because a 78-review game literally NAMED `City Builder` is more
topically related to that string. Qwen3's top three are Cities: Skylines II
(73,524 reviews), TheoTown (3,309) and Kingdoms Reborn (9,113) - all real,
well-regarded city builders. So the `core` gain is not a labelling artifact: the
tier moved 17.8 -> 27.2 AND the mechanism probe moved with it, which is the
first time those two have agreed in this project.

**The German result is the largest single movement, and the least trustworthy.**
Aggregate DE goes 42.6 -> 51.9, and `specific` DE goes 55.6 -> 77.8 while the
EN/DE gap on that tier collapses from 30.2 to 13.7. README calls German the
project's second-biggest weakness and says "a German document field is the fix
and it is not built" - it may not need to be. But `specific` DE is **n=9**, so
77.8% against 55.6% is seven queries against five, a two-query swing, and this
file's own floor is ~2.5 points at n=44. Treat it as a strong hint worth
building a bigger German set for, NOT as a 22-point result.

**Latency is 4x, 1,021ms against bge's 264ms**, and it was nearly recorded
wrong. The first run reported p95 6,234ms against a 1,105ms median, which is not
a tail - it is one query. The first batch of a given SHAPE pays CUDA kernel
selection: 9,668ms for the first 200-pair call against a 963ms steady state,
landing on whichever query happened to go first. `verify_rerank_model()` was
warming up with a 2-pair probe, which does not trigger the same kernels. Warming
at full pool size moves p95 to 1,376ms. A warm-up that does not match the real
shape is not a warm-up.

**What to take from this.** #36 was right to record 8.1% as NOT MEASURED instead
of as a verdict, and that discipline is the only reason this was ever revisited -
a table saying "Qwen3: 8.1%" would have closed the question permanently. The
cheap check that caught it was comparing SCORE SPREAD on a known-ordered triple,
which takes one command and would have flagged the mis-invocation before an eval
ever ran. Do that before believing any reranker's recall number.

**Correction, same day, after being asked to be sure.** The tables above are
point estimates and I presented them as findings. A paired bootstrap over
queries (10,000 resamples) and a sign test say only ONE of the three
comparisons survives:

| comparison | diff | 95% CI | sign test |
| --- | --- | --- | --- |
| qwen3 - baseline | +8.3% | **[+2.5%, +14.5%]** | 15-4, p=0.0096 |
| bge - baseline | +5.6% | [-0.1%, +11.9%] | 13-7, p=0.132 |
| **qwen3 - bge** | **+2.7%** | **[-2.1%, +7.6%]** | 11-5, p=0.105 |

**Qwen3 and bge are NOT distinguishable on recall** - not overall, not on
`core` (+7.2% [-1.7%, +16.1%]), not on `specific` (+0.0%), not on German. 102
of the 118 queries return identical recall for the two models; the entire
difference is 11 wins against 5 losses, and 12-4 would have been needed for
p<0.05. #36's headline "+5.6 points" for bge over the two-stage baseline is
marginal by the same test, with a lower bound of -0.1%.

What survives: **reranking beats not reranking**, with Qwen3 at
+8.3% [+2.5%, +14.5%] overall and +9.1% [+2.3%, +18.2%] on `specific`. That is
the claim this work supports.

So the model choice cannot be made on recall, and the tiebreak is the mechanism
probe, which is DETERMINISTIC rather than sampled: bge drops Cities: Skylines II
from rank 1 to 37 for "city builder" and Qwen3 holds it at 1. That is a
reproducible behaviour on a query class the eval cannot score, not a
sampling artifact - and it costs 4x the latency, 1,021ms against 264ms. Qwen3
stays the default because latency was explicitly not a constraint here; on a
latency budget bge is the same recall for a quarter of the cost.

The German result specifically does NOT survive and should not be repeated:
5-2 on discordant queries, p=0.227. It remains a reason to build a bigger
German set, which was already the conclusion, and nothing more.

**What to take from this.** Three of these arms were reported as results before
anyone asked whether they could be told apart, and n=118 with ~100 ties has far
less power than a 118-query eval sounds like it has. This file's stated floor
("~1 point at n=118") came from EMBEDDING reproducibility - re-running the same
config - and that is a different and much smaller quantity than the uncertainty
in a difference between two configs. Reproducible is not the same as
distinguishable. Run the paired test before writing the table, not after being
challenged on it.

---

### 38. The hallucination checker hallucinated, three separate ways (2026-09-08)

**The feature is a one-line "why this matches" per result; the deliverable is
the discard rate.** An LLM asked to justify a search result will claim a game is
`Souls-like` because the sentence reads better that way, and a plausible
sentence attached to a real game is the hardest kind of wrong to notice - it
looks exactly like the feature working. So `app/explain.py` verifies every claim
against `games.tags`, discards what fails, and `eval/run_explain_eval.py` counts
how often.

Three checks: the returned `app_id` must be one we asked about, `cited_tags`
must be a subset of the game's real tags, and any tag NAMED IN THE PROSE must be
one the game has.

**The first honest number was wrong, and only an audit found it.** The initial
full run reported 7.3% discarded over 590 explanations. Printing eight discards
next to each game's real tags - rather than trusting the count - showed three of
the eight were the checker's fault, not the model's:

```
Garden Life: A Cozy Simulator     PROSE but absent: ['Fishing']
  "It is a Cozy Farming Sim, but does not include Fishing."

Little Witch in the Woods         PROSE but absent: ['Experience']
  "Experience the daily life of an apprentice witch..."
```

Three distinct false-positive classes, all inflating the rate:

1. **Overlapping tags.** `Farming` and `Farming Sim` are both real tags, so "it
   is a Farming Sim" matched BOTH, and a game carrying only the longer one was
   accused of citing the shorter. This one fired on the very first live run -
   two of five *correct* explanations discarded. Fixed by resolving matches
   longest-first and dropping any span contained in a longer one.
2. **Negation.** The prompt tells the model to hedge rather than invent, so "but
   does not include Fishing" is the model OBEYING - and the checker punished it
   for saying the word. Fixed with a negation guard scoped to the tag's own
   clause, because "not a puzzle game but it is Souls-like" must still flag
   `Souls-like`. CLAUDE.md already carried this exact lesson for
   `wants_singleplayer()`; it applies to any regex over prose, not just queries.
3. **Sentence-initial capitalisation.** `Experience` is a real tag and also an
   ordinary verb. Case-sensitivity was supposed to separate them, and it does
   mid-sentence, but a capital at position 0 is grammar rather than a citation.
   Single-word tags no longer count sentence-initially; multi-word ones still
   do, since "Open World games are..." really does name one.

The genuine catches in the same audit were real and worth having: `Random
Dungeons` (not even a tag in the 452-tag vocabulary - the model invented the
name), `Bikes` claimed for a game tagged `Automobile Sim`, and `Floating` for a
city-builder.

**Corrected rate: 4.6% over 590 explanations**, down from 7.3%. The prose
check alone went from 19 discards to 3, so **16 of the original 43 were the
checker's fault rather than the model's** - 37% of the reported failures.
Split: unlisted_tag 2.7% (16), prose_tag 0.5% (3), missing 1.4% (8), median
2.5s per batch of five. The two untouched checks are bit-identical across the
runs, which is what says the change did what it claimed and nothing else.

**The self-test is the reason any of this is trustworthy.**
`run_explain_eval.py --self-test` monkeypatches four known-bad responses -
citing an absent tag, naming one only in prose, returning an app_id nobody asked
about, and raising outright - and requires each to be caught. It also runs a
CONTROL with a real response, because a verifier that rejects everything would
otherwise score a perfect self-test. All four arms were caught before the first
real number was taken, which is what made the false positives findable: the
checker was known to fire correctly on lies, so a suspicious rate had to be
investigated rather than explained away.

**What to take from this.** A verifier is a measuring instrument and gets
measured like one. Every one of these three bugs pushed the number UP, which is
the safe-looking direction - a hallucination rate that reads too high looks like
diligence and nobody audits it. The check that caught them was printing the
evidence next to the ground truth and reading eight of them, which took one
script and less time than writing this entry. The reported number is also a
FLOOR rather than a measure: it catches invented TAGS, and a model that invents
a plot detail out of the description passes every check here.

---

### 39. The relaxation ladder's important half is the part that does nothing (2026-09-08)

**BUILD_PLAN's Weekend 4 item 5.** A query whose filters match almost nothing
returned an almost-empty page and a message telling the user to fix it. Now the
filters widen one constraint at a time until the page fills, and the response
says what was given up.

**It runs on COUNTS, not on retried searches, and that is the whole cost
argument.** The obvious implementation re-runs `search()` after each relaxation,
which since #36 costs ~1.1s of cross-encoder per attempt - three seconds spent
deciding which filters to use. A capped count over the same `_apply_filters()`
is 11-24ms:

```
no filters            capped count 200    24ms
tags+price+platform   capped count 200    13ms
very selective        capped count   2    11ms
zero rows             capped count   0    11ms
```

The cap matters: uncapped, the unfiltered count is 611ms, because "how many"
is a much harder question than "are there at least ten". So the ladder is
walked on counts and exactly one real search runs at the end. A query that
needs no relaxation pays one count and nothing else - measured at 1,067ms total
against the usual ~1,080ms.

**The never-relax list is the important half of the file.** A short page is a
disappointment; a confidently wrong page is a defect. Not relaxed, ever:
`max_required_age` (a safety constraint - "for a 7 year old"), `excluded_tags`
(dropping it shows horror to someone who said nothing scary),
`excluded_app_ids` (returns the game they excluded by name), `multiplayer`
(returns the wrong KIND of game, which is #32 arriving by another route), and
`platforms` (a compatibility fact - "here are some Windows games anyway" is
worthless to a Linux user). The loop gives up and returns a short page instead.

Verified as behaviour rather than by reading the list: a query setting all five
plus a price and a year returned results having relaxed ONLY `released_after`,
with age, exclusions, platforms and the multiplayer axis all intact.

**No model is in the loop, and that is the point.** An agent would ask the LLM
which constraint to drop. This asks a table, in a fixed order, with a stopping
condition. It is reproducible, testable, free, and cannot invent a constraint
that was never there - which is a better demonstration of understanding agents
than using one would be.

**The ladder order is a JUDGEMENT and is labelled as one.** required_tags first
because CLAUDE.md already calls it "a coarse recall gate" whose job the vector
does better and whose intent survives whole in `semantic_query`; then
min_reviews, released_after, min_price; then max_price DOUBLED rather than
dropped, twice, which is BUILD_PLAN's own example and keeps half the intent.
There is no eval for "was that the right constraint to give up" and inventing
one would need labels nobody has, so it is stated as judgement in the code
rather than dressed up as tuned.

**Two things the first test run got wrong, both mine.** The first "starved"
query I wrote was not starved at all - `Cozy` AND `Horror` AND `Investigation`
has been ANY-of since #33, so it matched plenty and the ladder correctly did
nothing. I briefly read that as a bug in the relaxation. And the notes were
written with em dashes, which a Windows console renders as a replacement
character; the same string is printed by the CLI and rendered in the browser, so
it is ASCII now.

`run_eval` sets `relax_filters=False` and prints `relax: OFF` on its
self-labelling line. Recall is measured against a FIXED filter set, and a
harness that quietly widened filters whenever a query returned little would
report the relaxation as retrieval quality. The no-parse path would never
trigger it - no filters means all 55,120 rows pass - but `--parse` would, and a
number that only sometimes includes a second mechanism is the worst kind.
Confirmed unchanged at 68.8 / 27.2 / 88.6 / 77.3.

---

### 40. A p95 guard that permitted exactly what it forbade (2026-09-08)

**Item 6 is observability, and the deliverable is not the dashboard - it is that
the numbers on it cannot be quoted wrongly.** `/api/stats` reports p50 and p95
per pipeline stage over a ring buffer of the last 500 requests. The obvious risk
with a hand-run panel is small n: with 9 requests behind it, "p95" is a
confident-looking label on a value that has no right to it.

So p95 is withheld below a threshold. I set the threshold to 20 by eye and wrote
a check script that asserted p95 was non-null once past it. That assertion
passed. What it printed did not:

```
n=20      p50=110.0 p95=119.0 max=119.0  (p95 appears)
```

p95 and max are the same number. At n=20 the guard was returning the maximum
under a p95 label - the precise thing it existed to prevent - and the assertion
never noticed because it only checked for non-null.

**Mechanism.** Nearest-rank, the same expression `run_eval` uses, is
`ordered[min(n - 1, int(n * 0.95))]`. That index equals `n - 1` for every n up
to 20, because `int(20 * 0.95) = int(19.0) = 19 = n - 1`. The first n at which
p95 stops being the maximum is **21**. Enumerated rather than reasoned about:

```
n=  19  p95 index= 18  last index= 18  == MAX
n=  20  p95 index= 19  last index= 19  == MAX
n=  21  p95 index= 19  last index= 20  ok
```

**Fixed** by `MIN_P95_SAMPLES = 21` and two assertions that make the property
structural rather than incidental: `p95 < max` at the threshold, and a derived
check that recomputes the boundary from the formula and requires the constant to
match it. If the percentile method ever changes, the second one fails.

**What to take from this.** This is #38's lesson arriving in a new place: a
guard that has never been *shown* to fire is not known to work, and the way to
show it is to print the evidence beside the thing it is supposed to differ from.
Asserting "p95 is not null" tested that the code ran. Printing p95 next to max
tested what it meant. The gap between those two is where the bug lived, and it
was one column of output wide.

A second, smaller version of the same thing in the same session: `note()` counts
events, and with a deliberately broken `CHAT_MODEL` a single search reported
`parse_call_failed: 2`. Both were real - `_warm_models()` parses at startup and
that call failed too - but the count is of parser CALLS, not requests, so
dividing it by `search.n` would give a rate above 100%. Counting the startup
failure is right; presenting it next to a request count without saying so is not.
