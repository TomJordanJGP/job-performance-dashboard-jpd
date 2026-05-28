-- Daily incremental sync: re-sync last 5 days from source to JPD.t04_vacancy_events.
-- Delete + re-insert window is idempotent and catches late-arriving rows.
-- Ported verbatim from prod's incremental_sync_combined.sql, with two adjustments:
--   1. Retargeted at JPD.t04_vacancy_events (partitioned, clustered).
--   2. Derives event_date_dt (DATE) from event_date (STRING 'YYYYMMDD') at INSERT time
--      so the partition column is populated for new rows.
--
-- Prerequisite: scripts/04_create_and_backfill_ga4.sql has been run once to create the table.
-- Safe to run repeatedly.

-- Step 1: Delete last 5 days from destination
DELETE FROM `site-monitoring-421401.JPD.t04_vacancy_events`
WHERE event_date_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 5 DAY);

-- Step 2: Re-insert last 5 days (plus any new dates) from source
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
FROM `jobsgopublic.Datastudio_scheduled_data_combined.Job-performance-detaile_combined` AS src
WHERE src.event_date >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 5 DAY));
