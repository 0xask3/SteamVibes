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
