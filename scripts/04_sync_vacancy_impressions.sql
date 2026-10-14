-- scripts/04_sync_vacancy_impressions.sql
-- DAILY: refresh the trailing 7 days of JPD.t04_vacancy_impressions — how often each
-- vacancy was shown, per day, site and channel. Delete + re-insert the window so late
-- GA4 export days and Google's 2-3 day lag / restatements are absorbed (same shape as
-- 04_sync_gsc_daily.sql). One-off history: 04_create_and_backfill_vacancy_impressions.sql.
--
-- channel = 'onsite': the vacancy was listed in Jobs Go Public / LG Jobs search results.
--   GA4 has no impression event: each client-side `job_search` event's `entity_id`
--   param lists the vacancies on that results page ('99340 | 97780 | ...', ~10). A
--   single-result page sends the id as an INT and a no-results page sends -1, hence
--   the COALESCE and the > 0 filter. Read from the raw GA4 exports (JGP property
--   484716628, LG Jobs 489117631): the Datastudio Search_impressions_all tables are
--   unusable — both per-site queries read the JGP property, so they hold JGP's
--   impressions under both labels and none of LG Jobs' (verified 2026-10-14).
--   Server-side GA4 can't be used either: ss_job_search sends entity_id = -1.
-- channel = 'google': the vacancy's job page (/job/<slug>-<id>) was shown in Google
--   search, from Search Console's per-URL export. All search types and countries, as
--   in 04_sync_gsc_daily.sql. Homepage / listing / blog impressions name no vacancy
--   and stay in the site-level t04_gsc_daily.
--
-- `_TABLE_SUFFIX NOT LIKE 'intraday%'`: the GA4 wildcard also matches today's
-- events_intraday_YYYYMMDD table, and 'intraday...' sorts after any date so a `>=`
-- alone would let it in. ~0.8 GB per run.

DELETE FROM `site-monitoring-421401.JPD.t04_vacancy_impressions`
WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY);

INSERT INTO `site-monitoring-421401.JPD.t04_vacancy_impressions`
  (event_date, site, channel, entity_id, impressions)
WITH
search_lists AS (
  SELECT 'Jobs Go Public' AS site, event_date,
    (SELECT COALESCE(value.string_value, CAST(value.int_value AS STRING))
     FROM UNNEST(event_params) WHERE key = 'entity_id') AS ids
  FROM `jobsgopublic.analytics_484716628.events_*`
  WHERE _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY))
    AND _TABLE_SUFFIX NOT LIKE 'intraday%'
    AND event_name = 'job_search'
  UNION ALL
  SELECT 'LG Jobs', event_date,
    (SELECT COALESCE(value.string_value, CAST(value.int_value AS STRING))
     FROM UNNEST(event_params) WHERE key = 'entity_id')
  FROM `jobsgopublic.analytics_489117631.events_*`
  WHERE _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY))
    AND _TABLE_SUFFIX NOT LIKE 'intraday%'
    AND event_name = 'job_search'
),
onsite AS (
  SELECT PARSE_DATE('%Y%m%d', event_date) AS event_date, site, 'onsite' AS channel,
    SAFE_CAST(TRIM(id) AS INT64) AS entity_id, COUNT(*) AS impressions
  FROM search_lists, UNNEST(SPLIT(ids, '|')) AS id
  WHERE SAFE_CAST(TRIM(id) AS INT64) > 0
  GROUP BY 1, 2, 3, 4
),
google_pages AS (
  SELECT data_date, 'Jobs Go Public' AS site, url, impressions
  FROM `jobsgopublic.searchconsole_jobsgopublic.searchdata_url_impression`
  WHERE data_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  UNION ALL
  SELECT data_date, 'LG Jobs', url, impressions
  FROM `jobsgopublic.searchconsole_lgjobs.searchdata_url_impression`
  WHERE data_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
),
google AS (
  SELECT data_date AS event_date, site, 'google' AS channel,
    SAFE_CAST(REGEXP_EXTRACT(url, r'/job/[^/?#]*-(\d+)(?:[/?#]|$)') AS INT64) AS entity_id,
    SUM(impressions) AS impressions
  FROM google_pages
  WHERE SAFE_CAST(REGEXP_EXTRACT(url, r'/job/[^/?#]*-(\d+)(?:[/?#]|$)') AS INT64) IS NOT NULL
  GROUP BY 1, 2, 3, 4
)
SELECT * FROM onsite
UNION ALL
SELECT * FROM google;
