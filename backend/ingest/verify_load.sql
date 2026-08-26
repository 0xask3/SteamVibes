-- Health check for the games.json ingest.
--
--   docker compose exec -T db psql -U steam -d steamvibe -f - < backend/ingest/verify_load.sql
--
-- Every row should read OK. Expected counts are the verified figures from the
-- 2026-08-26 load; they only change if the source file is replaced.

WITH checks AS (
    -- Row counts. A short count means the loader silently dropped records.
    SELECT 1 AS ord, 'games rows' AS check_name,
           (SELECT count(*) FROM games)::text AS actual, '138964' AS expected
    UNION ALL SELECT 2, 'game_tags rows',
           (SELECT count(*) FROM game_tags)::text, '1180522'
    UNION ALL SELECT 3, 'game_genres rows',
           (SELECT count(*) FROM game_genres)::text, '376325'
    UNION ALL SELECT 4, 'game_categories rows',
           (SELECT count(*) FROM game_categories)::text, '611783'

    -- Embedding scope: everything with a description or tags.
    UNION ALL SELECT 5, 'rows with embed_text',
           (SELECT count(*) FROM games WHERE embed_text IS NOT NULL)::text, '130651'

    -- Parsing. Nonzero here means a whole column is quietly wrong.
    UNION ALL SELECT 6, 'unparsed release dates',
           (SELECT count(*) FROM games WHERE release_date IS NULL)::text, '0'
    UNION ALL SELECT 7, 'null prices',
           (SELECT count(*) FROM games WHERE price_usd IS NULL)::text, '0'
    UNION ALL SELECT 8, 'blank names',
           (SELECT count(*) FROM games WHERE name = '')::text, '1'

    -- Price derivation: list price can never be below the sale price.
    UNION ALL SELECT 9, 'list_price < sale price',
           (SELECT count(*) FROM games
             WHERE list_price_usd IS NOT NULL
               AND list_price_usd < price_usd)::text, '0'
    UNION ALL SELECT 10, 'discounted games',
           (SELECT count(*) FROM games WHERE discount_pct > 0)::text, '41751'
    UNION ALL SELECT 11, 'discount out of range',
           (SELECT count(*) FROM games
             WHERE discount_pct < 0 OR discount_pct > 100)::text, '0'

    -- Tag vocabulary. 452 distinct tags, top vote count from Counter-Strike 2.
    UNION ALL SELECT 12, 'distinct tags',
           (SELECT count(DISTINCT tag) FROM game_tags)::text, '452'
    UNION ALL SELECT 13, 'max tag votes',
           (SELECT max(votes) FROM game_tags)::text, '102594'
    UNION ALL SELECT 14, 'tags with 0 or negative votes',
           (SELECT count(*) FROM game_tags WHERE votes <= 0)::text, '0'

    -- Referential integrity. The FKs make these impossible; proving it is free.
    UNION ALL SELECT 15, 'orphaned tag rows',
           (SELECT count(*) FROM game_tags t
             LEFT JOIN games g ON g.app_id = t.app_id
            WHERE g.app_id IS NULL)::text, '0'

    -- Consistency between the two representations of "has tags".
    UNION ALL SELECT 16, 'embed_text but no source text',
           (SELECT count(*) FROM games g
            WHERE g.embed_text IS NOT NULL
              AND g.short_description IS NULL
              AND NOT EXISTS (SELECT 1 FROM game_tags t
                               WHERE t.app_id = g.app_id))::text, '0'

    -- Date sanity. Steam launched in 2003; pre-2003 entries are back-catalogue
    -- re-releases, which is why the floor is 1997 rather than 2003.
    UNION ALL SELECT 17, 'release dates before 1997',
           (SELECT count(*) FROM games WHERE release_date < '1997-01-01')::text, '0'
    UNION ALL SELECT 18, 'release dates after 2030',
           (SELECT count(*) FROM games WHERE release_date > '2030-01-01')::text, '0'
)
SELECT check_name,
       actual,
       expected,
       CASE WHEN actual = expected THEN 'OK' ELSE '*** FAIL ***' END AS status
FROM checks
ORDER BY ord;
