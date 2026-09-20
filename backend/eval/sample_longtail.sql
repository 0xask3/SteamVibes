-- Candidate targets for a GENUINE long-tail eval tier.
--
-- Every core target in queries.yaml has >=11,267 reviews, so recall there rises
-- with the popularity weight until the long tail is gone - it cannot see the
-- cost of the thing it measures (failures.md #26).
--
-- `ORDER BY md5(app_id::text)` is load-bearing. The first version ordered by
-- total_reviews DESC inside the band, which returns the top EDGE of the tail:
-- those targets sat at the 93rd-98th percentile and rose with the weight
-- instead of falling. This picks arbitrarily but reproducibly, with no
-- correlation to review count.
--
-- WHERE THE TAIL ACTUALLY IS, among the 55,120 games above REVIEW_THRESHOLD=10:
--     11-49 reviews  24,483 (44%)     1k-5k    4,482  (8%)
--     50-199         13,966 (25%)     5k+      2,732  (5%)
--     200-999         9,457 (17%)
-- The median searchable game has ~90 reviews. 30-300 brackets it; anything
-- above 1,000 is the top 8% and is not what this file is for.
--
-- HOW TO USE IT
-- 1. CHECK THE PCTILE COLUMN on what you picked. If the median is above ~90 you
--    are labelling the head again and the tier will be worthless. This is the
--    rule that does the work - the 22 queries built from this file after the
--    md5 fix produced the project's first honest peak-and-fall curve, and the
--    only thing that changed was the sampling. See failures.md #28.
-- 2. Pick the game FIRST, then write the query someone would type wanting it.
--    Never the other way round: writing the query first and hunting for a match
--    selects for games the ranker already returns.
-- 3. Prefer not to paraphrase the blurb, but do not believe this buys much.
--    `short_description` is inside embed_text, so a near-copy lands the target
--    at cosine rank ~1. Deliberately writing "in a player's words" instead was
--    measured and changed nothing: 37% content-word overlap either way, and
--    MORE targets at rank 1 than the contaminated tier it replaced. It works
--    regardless, because an obscure target at rank 1 is at the bottom of the
--    popularity ranking and the weight pushes it down, where a famous one gains
--    twice. Obscurity prices the weight; prose does not.
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
