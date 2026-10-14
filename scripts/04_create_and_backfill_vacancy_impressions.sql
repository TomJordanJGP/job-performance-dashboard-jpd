-- One-off: (re)create JPD.t04_vacancy_impressions and backfill all history (~26 GB scan).
-- After this, scripts/04_sync_vacancy_impressions.sql refreshes the trailing 7 days; its
-- header explains the sources and why. Keep the two SELECTs in step.
--
-- History starts where the sources do: GA4 exports 2025-04-28 (JGP) / 2025-05-15 (LG Jobs),
-- with JGP missing 3-4 May and 10 May-15 Jun 2025 and LG Jobs 9 days in May-Jun 2025;
-- Search Console per-URL export from 2025-06-17.

CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t04_vacancy_impressions`
(
  event_date DATE,
  site STRING,
  channel STRING OPTIONS (description = "onsite = listed in Jobs Go Public / LG Jobs search results (GA4 job_search entity_id lists); google = the vacancy's job page shown in Google search (Search Console per-URL export)"),
  entity_id INT64,
  impressions INT64
)
PARTITION BY event_date
CLUSTER BY entity_id
OPTIONS (
  description = "Vacancy impressions per day x site x channel. Sources: raw GA4 exports jobsgopublic.analytics_484716628 (JGP) / analytics_489117631 (LG Jobs) and searchconsole_{jobsgopublic,lgjobs}.searchdata_url_impression. Refresh: daily 7-day delete+insert via scripts/04_sync_vacancy_impressions.sql."
);

INSERT INTO `site-monitoring-421401.JPD.t04_vacancy_impressions`
  (event_date, site, channel, entity_id, impressions)
WITH
search_lists AS (
  SELECT 'Jobs Go Public' AS site, event_date,
    (SELECT COALESCE(value.string_value, CAST(value.int_value AS STRING))
     FROM UNNEST(event_params) WHERE key = 'entity_id') AS ids
  FROM `jobsgopublic.analytics_484716628.events_*`
  WHERE _TABLE_SUFFIX NOT LIKE 'intraday%'
    AND event_name = 'job_search'
  UNION ALL
  SELECT 'LG Jobs', event_date,
    (SELECT COALESCE(value.string_value, CAST(value.int_value AS STRING))
     FROM UNNEST(event_params) WHERE key = 'entity_id')
  FROM `jobsgopublic.analytics_489117631.events_*`
  WHERE _TABLE_SUFFIX NOT LIKE 'intraday%'
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
  UNION ALL
  SELECT data_date, 'LG Jobs', url, impressions
  FROM `jobsgopublic.searchconsole_lgjobs.searchdata_url_impression`
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
