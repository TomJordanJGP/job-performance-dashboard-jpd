-- scripts/04_create_and_backfill_gsc.sql
-- ONE-OFF: create + backfill JPD.t04_gsc_daily (per-day Search Console metrics).
--
-- Pre-aggregates Google's GSC BigQuery exports (jobsgopublic.searchconsole_*)
-- down to one row per day, split JGP / LG. t06_summary_daily_totals then JOINs
-- this small table (~400 rows) instead of re-scanning ~5 GB of raw GSC history
-- on every rebuild. Kept fresh thereafter by 04_sync_gsc_daily.sql (trailing
-- 7-day delete+insert). Mirrors the GA4 create-and-backfill / sync split.
--
-- Source tables are PARTITION BY data_date; this one-off scans full history
-- (~5 GB). DO NOT re-run as part of the daily pipeline — use the sync instead.
--
-- Run:  client.query(open('scripts/04_create_and_backfill_gsc.sql').read()).result()

CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t04_gsc_daily`
PARTITION BY event_date
AS
WITH
-- Site-level metrics per day (impressions, clicks, position — total + GB-only).
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
    GROUP BY data_date
  ) lg ON jgp.data_date = lg.data_date
),
-- URL-level rich-result counts per day (job listing + job detail appearances).
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
    GROUP BY data_date
  ) jgp
  FULL OUTER JOIN (
    SELECT data_date,
      SUM(IF(is_job_listing, impressions, 0)) AS job_listing_rich,
      SUM(IF(is_job_details, impressions, 0)) AS job_detail_rich
    FROM `jobsgopublic.searchconsole_lgjobs.searchdata_url_impression`
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
