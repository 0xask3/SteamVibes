-- Spot-check loaded data against real Steam pages.
--
--   docker compose exec -T db psql -U steam -d steamvibe -f - < backend/ingest/spot_check.sql
--
-- Open each steam_url and compare: name, release date, price, platforms,
-- review counts, top tags. The only check that catches wrong ASSUMPTIONS
-- rather than wrong parsing, because it compares against the outside world.
--
-- \x auto makes wide rows readable in psql.
\x auto

SELECT g.name,
       g.release_date,
       g.list_price_usd                      AS normally_costs,
       g.price_usd                           AS scraped_at,
       g.discount_pct                        AS pct_off,
       concat_ws(' / ',
                 CASE WHEN g.windows THEN 'Windows' END,
                 CASE WHEN g.mac     THEN 'Mac'     END,
                 CASE WHEN g.linux   THEN 'Linux'   END)   AS platforms,
       g.positive_reviews,
       g.negative_reviews,
       round(100.0 * g.positive_reviews / NULLIF(g.total_reviews, 0))
                                             AS pct_positive,
       array_to_string(g.developers, ', ')   AS developer,
       (SELECT string_agg(x.tag, ', ' ORDER BY x.votes DESC)
          FROM (SELECT tag, votes
                  FROM game_tags
                 WHERE app_id = g.app_id
                 ORDER BY votes DESC
                 LIMIT 8) x)                 AS top_tags,
       'https://store.steampowered.com/app/' || g.app_id AS steam_url
FROM games g
WHERE g.app_id IN (
    413150,   -- Stardew Valley
    105600,   -- Terraria
    892970,   -- Valheim
    1145360,  -- Hades
    620,      -- Portal 2
    292030    -- The Witcher 3
)
ORDER BY g.total_reviews DESC;


-- Unbiased version: 5 random well-reviewed games. Re-run for a new sample.
-- Checking games someone else chose is a weaker test than checking games
-- chosen at random.
SELECT g.name,
       g.release_date,
       g.list_price_usd                      AS normally_costs,
       g.discount_pct                        AS pct_off,
       concat_ws(' / ',
                 CASE WHEN g.windows THEN 'Windows' END,
                 CASE WHEN g.mac     THEN 'Mac'     END,
                 CASE WHEN g.linux   THEN 'Linux'   END)   AS platforms,
       g.positive_reviews,
       g.negative_reviews,
       (SELECT string_agg(x.tag, ', ' ORDER BY x.votes DESC)
          FROM (SELECT tag, votes
                  FROM game_tags
                 WHERE app_id = g.app_id
                 ORDER BY votes DESC
                 LIMIT 6) x)                 AS top_tags,
       'https://store.steampowered.com/app/' || g.app_id AS steam_url
FROM games g
WHERE g.total_reviews > 5000
ORDER BY random()
LIMIT 5;
