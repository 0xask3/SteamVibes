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
