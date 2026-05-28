-- One-off: create JPD.t04_vacancy_events (partitioned + clustered) and backfill all history.
-- Run ONCE. After this, scripts/04_sync_ga4_events.sql handles the daily 5-day window.
--
-- Why this exists separately from the daily sync:
--   Prod's incremental_sync_combined.sql assumes the destination already has full history.
--   JPD is a fresh dataset — the destination table doesn't exist yet, so we seed it here.
--
-- Why partition by a derived DATE column (not the raw STRING event_date):
--   Source carries event_date as STRING 'YYYYMMDD'. To partition we need a true DATE.
--   We add a sibling event_date_dt DATE column derived at ingest. Original STRING column
--   is preserved so downstream SQL (prod's enriched/aggregate scripts) keeps working
--   unchanged when ported.

CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t04_vacancy_events`
(
  event_name STRING,
  event_date STRING,
  event_date_dt DATE,
  hour_of_day STRING,
  entity_id INT64,
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
  description = "GA4 vacancy events. Source: jobsgopublic.Datastudio_scheduled_data_combined.Job-performance-detaile_combined. Refresh: daily 5-day delete+insert window via scripts/04_sync_ga4_events.sql. Partition: event_date_dt (derived DATE). Cluster: entity_id, organization_id."
);

INSERT INTO `site-monitoring-421401.JPD.t04_vacancy_events`
(
  event_name, event_date, event_date_dt, hour_of_day, entity_id, entity_type, entity_subtype,
  organization_name, title, application_type, occupations, regions, employment_types,
  importer_ID, current_user_id, user_role, owner_id, organization_id,
  page_referrer, page_location, upgrades, ats_vacancy_number, ats_account_number,
  salary_currency, salary_low, salary_high, device, operating_system, browser,
  campaign, medium, source, Events, site
)
SELECT
  event_name,
  event_date,
  SAFE.PARSE_DATE('%Y%m%d', event_date) AS event_date_dt,
  hour_of_day, entity_id, entity_type, entity_subtype,
  organization_name, title, application_type, occupations, regions, employment_types,
  importer_ID, current_user_id, user_role, owner_id, organization_id,
  page_referrer, page_location, upgrades, ats_vacancy_number, ats_account_number,
  salary_currency, salary_low, salary_high, device, operating_system, browser,
  campaign, medium, source, Events, site
FROM `jobsgopublic.Datastudio_scheduled_data_combined.Job-performance-detaile_combined`;
