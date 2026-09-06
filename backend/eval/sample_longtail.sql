-- Candidate targets for a GENUINE long-tail eval tier.
--
-- WHY THIS EXISTS
-- All 37 core app_ids in queries.yaml have >=11,267 reviews, so recall@10 rises
-- with the popularity weight until the long tail is gone - it cannot see the
-- cost of the thing it measures. See failures.md #26.
--
-- THE FIRST VERSION OF THIS FILE DID NOT FIX THAT. It used
--     SELECT DISTINCT ON (g.tags[1]) ... ORDER BY g.tags[1], total_reviews DESC
-- which returns the MOST-reviewed game per tag inside the band, i.e. the top
-- edge of the tail. The 22 targets it produced sit at the 93rd-98th percentile
-- of the corpus (median 97.1), so the tier they built rose with the popularity
-- weight instead of falling. `ORDER BY md5(app_id::text)` below is the fix:
-- an arbitrary but reproducible pick, with no correlation to review count.
--
-- WHERE THE TAIL ACTUALLY IS, among the 55,120 games above REVIEW_THRESHOLD=10:
--     11-49 reviews  24,483 (44%)     1k-5k    4,482  (8%)
--     50-199         13,966 (25%)     5k+      2,732  (5%)
--     200-999         9,457 (17%)
-- The median searchable game has ~90 reviews. 30-300 brackets it; anything
-- above 1,000 is the top 8% and is not what this file is for.
--
-- HOW TO USE IT
-- 1. Pick the game FIRST, then write the query someone would type wanting it.
--    Never the other way round: writing the query first and hunting for a match
--    selects for games the ranker already returns.
-- 2. Do NOT paraphrase the blurb. `short_description` is part of embed_text, so
--    a near-copy lands the target at cosine rank ~1, where a popularity term is
--    too small to dislodge it - the tier then reports "no harm" by
--    construction, which is exactly how the first attempt failed. Read the
--    blurb, then write the query in the words a player would use six months
--    after finishing the game.
-- 3. Check the pctile column on what you picked. If the median is above ~90,
--    resample; you are labelling the head again.
--
--   docker compose exec -T db psql -U steam -d steamvibe -f - \
--     < backend/eval/sample_longtail.sql

WITH corpus AS (
    SELECT app_id,
           round((100 * cume_dist() OVER (ORDER BY total_reviews))::numeric, 1)
               AS pctile
    FROM games
    WHERE embedding IS NOT NULL
)
SELECT DISTINCT ON (g.tags[1])
    g.app_id,
    g.tags[1]                                            AS primary_tag,
    left(g.name, 34)                                     AS name,
    g.total_reviews                                      AS revs,
    c.pctile,
    round(100.0 * g.positive_reviews / g.total_reviews)   AS pct,
    left(g.short_description, 72)                        AS blurb
FROM games g
JOIN corpus c USING (app_id)
WHERE g.embedding IS NOT NULL
  AND g.total_reviews BETWEEN 30 AND 300
  AND g.positive_reviews::float / g.total_reviews >= 0.80
  AND g.short_description IS NOT NULL
  AND array_length(g.tags, 1) >= 5
ORDER BY g.tags[1], md5(g.app_id::text);
