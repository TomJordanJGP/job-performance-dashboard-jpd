-- scripts/04_sync_gsc_daily.sql
-- DAILY: refresh the trailing 7 days of JPD.t04_gsc_daily from Google's GSC
-- exports. Delete + re-insert the window so Google's 2-3 day lag and its
-- restatement of recent days are both absorbed (same shape as the GA4 sync).
--
-- The `WHERE data_date >= ...` on each raw scan prunes to recent partitions, so
-- this reads a few MB, not the ~5 GB the one-off backfill scans. Idempotent:
-- re-running re-derives the same window. Run after a fresh GSC export has landed
-- and BEFORE t06_summary_daily_totals (which joins this table).
--
-- Run:  client.query(open('scripts/04_sync_gsc_daily.sql').read()).result()

DELETE FROM `site-monitoring-421401.JPD.t04_gsc_daily`
WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY);

INSERT INTO `site-monitoring-421401.JPD.t04_gsc_daily` (
  event_date,
  impressions_jgp, impressions_lg, gb_impressions_jgp, gb_impressions_lg,
  gsc_clicks_jgp, gsc_clicks_lg, gb_gsc_clicks_jgp, gb_gsc_clicks_lg,
  sum_position_jgp, sum_position_lg,
  job_listing_rich_jgp, job_listing_rich_lg, job_detail_rich_jgp, job_detail_rich_lg
)
WITH
gsc_site_daily AS (
  SELECT
    COALESCE(jgp.data_date, lg.data_date) AS event_date,
    COALESCE(jgp.impressions, 0)    AS impressions_jgp,
    COALESCE(lg.impressions, 0)     AS impressions_lg,
    COALESCE(jgp.gb_impressions, 0) AS gb_impressions_jgp,
    COALESCE(lg.gb_impressions, 0)  AS gb_impressions_lg,
    COALESCE(jgp.clicks, 0)    AS gsc_clicks_jgp,
    COALESCE(lg.clicks, 0)     AS gsc_clicks_lg,
    COALESCE(jgp.gb_clicks, 0) AS gb_gsc_clicks_jgp,
    COALESCE(lg.gb_clicks, 0)  AS gb_gsc_clicks_lg,
    COALESCE(jgp.sum_pos, 0)   AS sum_position_jgp,
    COALESCE(lg.sum_pos, 0)    AS sum_position_lg
  FROM (
    SELECT data_date,
      SUM(impressions) AS impressions,
      SUM(IF(country = 'gbr', impressions, 0)) AS gb_impressions,
      SUM(clicks) AS clicks,
      SUM(IF(country = 'gbr', clicks, 0)) AS gb_clicks,
      SUM(sum_top_position) AS sum_pos
    FROM `jobsgopublic.searchconsole_jobsgopublic.searchdata_site_impression`
    WHERE data_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
    GROUP BY data_date
  ) jgp
  FULL OUTER JOIN (
    SELECT data_date,
      SUM(impressions) AS impressions,
      SUM(IF(country = 'gbr', impressions, 0)) AS gb_impressions,
      SUM(clicks) AS clicks,
      SUM(IF(country = 'gbr', clicks, 0)) AS gb_clicks,
      SUM(sum_top_position) AS sum_pos
    FROM `jobsgopublic.searchconsole_lgjobs.searchdata_site_impression`
    WHERE data_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
    GROUP BY data_date
  ) lg ON jgp.data_date = lg.data_date
),
gsc_rich_daily AS (
  SELECT
    COALESCE(jgp.data_date, lg.data_date) AS event_date,
    COALESCE(jgp.job_listing_rich, 0) AS job_listing_rich_jgp,
    COALESCE(lg.job_listing_rich, 0)  AS job_listing_rich_lg,
    COALESCE(jgp.job_detail_rich, 0)  AS job_detail_rich_jgp,
    COALESCE(lg.job_detail_rich, 0)   AS job_detail_rich_lg
  FROM (
    SELECT data_date,
      SUM(IF(is_job_listing, impressions, 0)) AS job_listing_rich,
      SUM(IF(is_job_details, impressions, 0)) AS job_detail_rich
    FROM `jobsgopublic.searchconsole_jobsgopublic.searchdata_url_impression`
    WHERE data_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
    GROUP BY data_date
  ) jgp
  FULL OUTER JOIN (
    SELECT data_date,
      SUM(IF(is_job_listing, impressions, 0)) AS job_listing_rich,
      SUM(IF(is_job_details, impressions, 0)) AS job_detail_rich
    FROM `jobsgopublic.searchconsole_lgjobs.searchdata_url_impression`
    WHERE data_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
    GROUP BY data_date
  ) lg ON jgp.data_date = lg.data_date
)
SELECT
  COALESCE(s.event_date, r.event_date) AS event_date,
  COALESCE(s.impressions_jgp, 0)    AS impressions_jgp,
  COALESCE(s.impressions_lg, 0)     AS impressions_lg,
  COALESCE(s.gb_impressions_jgp, 0) AS gb_impressions_jgp,
  COALESCE(s.gb_impressions_lg, 0)  AS gb_impressions_lg,
  COALESCE(s.gsc_clicks_jgp, 0)     AS gsc_clicks_jgp,
  COALESCE(s.gsc_clicks_lg, 0)      AS gsc_clicks_lg,
  COALESCE(s.gb_gsc_clicks_jgp, 0)  AS gb_gsc_clicks_jgp,
  COALESCE(s.gb_gsc_clicks_lg, 0)   AS gb_gsc_clicks_lg,
  COALESCE(s.sum_position_jgp, 0)   AS sum_position_jgp,
  COALESCE(s.sum_position_lg, 0)    AS sum_position_lg,
  COALESCE(r.job_listing_rich_jgp, 0) AS job_listing_rich_jgp,
  COALESCE(r.job_listing_rich_lg, 0)  AS job_listing_rich_lg,
  COALESCE(r.job_detail_rich_jgp, 0)  AS job_detail_rich_jgp,
  COALESCE(r.job_detail_rich_lg, 0)   AS job_detail_rich_lg
FROM gsc_site_daily s
FULL OUTER JOIN gsc_rich_daily r ON s.event_date = r.event_date;
