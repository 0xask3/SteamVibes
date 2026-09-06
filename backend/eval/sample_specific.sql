-- Candidate targets for the `specific` eval tier: the HEAD of the corpus.
--
-- WHY THIS EXISTS
-- `sample_longtail.sql` samples the tail; this samples its mirror image, and
-- the two are deliberately identical apart from the review band. failures.md
-- #28 claims `specific` and `tail` differ only in how popular their targets are,
-- which is what makes a weight sweep across the pair a controlled comparison.
-- That claim was not quite honest when it was written: the tail targets came
-- from a sampler and the specific ones were hand-picked, so target popularity
-- was not the only thing that varied. This file closes that gap.
--
-- Same DISTINCT ON, same md5 ordering, same percentile column, same tag and
-- description filters. ONLY the band changes:
--     sample_longtail.sql   30 - 300 reviews      37th-75th pctile searchable
--     this file          5,000 - 200,000        95th-99th pctile searchable
--
-- HOW TO USE IT - identical to sample_longtail.sql, and read that header first.
-- The one rule that does the work is checking the pctile column: this file is
-- SUPPOSED to return the head, so the check here is the opposite one. If the
-- median lands below ~93 you are sampling the middle and the pair stops being
-- a clean contrast.
--
--   docker compose exec -T db psql -U steam -d steamvibe -f - \
--     < backend/eval/sample_specific.sql

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
  AND g.total_reviews BETWEEN 5000 AND 200000
  AND g.positive_reviews::float / g.total_reviews >= 0.80
  AND g.short_description IS NOT NULL
  AND array_length(g.tags, 1) >= 5
ORDER BY g.tags[1], md5(g.app_id::text);
