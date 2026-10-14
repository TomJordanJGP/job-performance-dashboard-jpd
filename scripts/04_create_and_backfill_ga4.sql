-- One-off: (re)create JPD.t04_vacancy_events (partitioned + clustered) and backfill all history.
-- After this, scripts/04_sync_ga4_events.sql handles the daily 7-day window. Run the daily
-- refresh straight after a re-seed: its 04_recover_ga4_entity_ids.sql step re-matches the
-- id-less events across all history, which this script leaves NULL.
--
-- Why this exists separately from the daily sync:
--   The daily sync assumes the destination already has full history; this seeds it. Re-run
--   it to rebuild t04 from scratch, e.g. after a source change (last run 2026-10-14, when the
--   source moved to aggregated_data after the old combined table froze on 2026-08-26).
--
-- Why event_date is kept as STRING alongside the DATE event_date_dt:
--   The source's event_date is a DATE and becomes event_date_dt, the partition column. The
--   original source stored it as STRING 'YYYYMMDD', and downstream SQL ported from prod still
--   reads that form, so it's kept as a formatted copy.

CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t04_vacancy_events`
(
  event_name STRING,
  event_date STRING,
  event_date_dt DATE,
  hour_of_day STRING,
  entity_id INT64,
  entity_id_source STRING OPTIONS (description = "Where entity_id came from: ga4 (sent by GA4), url (job page URL), match_fingerprint / match_ats (inferred by scripts/04_recover_ga4_entity_ids.sql); NULL = unattributed"),
  entity_type STRING,
  entity_subtype STRING,
  organization_name STRING,
  title STRING,
  application_type STRING,
  occupations STRING,
  regions STRING,
  employment_types STRING,
  importer_ID INT64,
  current_user_id INT64,
  user_role STRING,
  owner_id INT64,
  organization_id INT64,
  page_referrer STRING,
  page_location STRING,
  upgrades STRING,
  ats_vacancy_number INT64,
  ats_account_number INT64,
  salary_currency STRING,
  salary_low INT64,
  salary_high INT64,
  device STRING,
  operating_system STRING,
  browser STRING,
  campaign STRING,
  medium STRING,
  source STRING,
  Events INT64,
  site STRING
)
PARTITION BY event_date_dt
CLUSTER BY entity_id, organization_id
OPTIONS (
  description = "GA4 vacancy events. Source: jobsgopublic.aggregated_data.ga4_job_events__jobsgopublic. Refresh: daily 7-day delete+insert window via scripts/04_sync_ga4_events.sql. Partition: event_date_dt (source DATE). Cluster: entity_id, organization_id."
);

INSERT INTO `site-monitoring-421401.JPD.t04_vacancy_events`
(
  event_name, event_date, event_date_dt, hour_of_day, entity_id, entity_id_source,
  entity_type, entity_subtype,
  organization_name, title, application_type, occupations, regions, employment_types,
  importer_ID, current_user_id, user_role, owner_id, organization_id,
  page_referrer, page_location, upgrades, ats_vacancy_number, ats_account_number,
  salary_currency, salary_low, salary_high, device, operating_system, browser,
  campaign, medium, source, Events, site
)
SELECT
  event_name,
  FORMAT_DATE('%Y%m%d', event_date) AS event_date,
  event_date AS event_date_dt,
  hour_of_day,
  -- Missing entity_id falls back to the id in the job page URL; see 04_sync_ga4_events.sql.
  COALESCE(entity_id, url_id) AS entity_id,
  CASE WHEN entity_id IS NOT NULL THEN 'ga4'
       WHEN url_id IS NOT NULL THEN 'url' END AS entity_id_source,
  entity_type, entity_subtype,
  organization_name, title, application_type, occupations, regions, employment_types,
  importer_ID, current_user_id, user_role, owner_id, organization_id,
  page_referrer, page_location, upgrades, ats_vacancy_number, ats_account_number,
  salary_currency, salary_low, salary_high, device, operating_system, browser,
  campaign, medium, source, Events,
  _source_sitename AS site
FROM (
  SELECT *, SAFE_CAST(REGEXP_EXTRACT(page_location, r'/job/[^/?#]*-(\d+)(?:[/?#]|$)') AS INT64) AS url_id
  FROM `jobsgopublic.aggregated_data.ga4_job_events__jobsgopublic`
  -- Rows without a date would land in t04's NULL partition, which the daily sync's DELETE
  -- never matches.
  WHERE event_date IS NOT NULL
);
