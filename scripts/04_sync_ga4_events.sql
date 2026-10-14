-- Daily incremental sync: re-sync last 7 days from source to JPD.t04_vacancy_events.
-- Delete + re-insert window is idempotent and catches late-arriving rows.
--
-- Source: jobsgopublic.aggregated_data.ga4_job_events__jobsgopublic (both sites, partitioned
-- by event_date DATE). It replaced Datastudio_scheduled_data_combined.Job-performance-detaile_combined,
-- which stopped updating after 2026-08-26; event totals match it exactly on the shared history.
-- It's rebuilt daily at ~06:30 UTC, rewriting D-5..D-2, so its newest day is D-2. The 7-day
-- window covers that rewrite with two days' margin for a missed run.
--
-- Mapped onto t04's existing schema so downstream SQL is unchanged:
--   event_date (DATE)  -> event_date_dt, plus event_date as STRING 'YYYYMMDD'
--   _source_sitename   -> site ('Jobs Go Public' / 'LG Jobs', same values as before)
--
-- Prerequisite: scripts/04_create_and_backfill_ga4.sql has been run once to create the table.
-- Safe to run repeatedly.

-- Step 1: Delete last 7 days from destination
DELETE FROM `site-monitoring-421401.JPD.t04_vacancy_events`
WHERE event_date_dt >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY);

-- Step 2: Re-insert last 7 days (plus any new dates) from source
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
  -- Since ~2026-08-10 GA4 sends a growing share of events without entity_id (~13% by
  -- Oct 2026, both sites). On job pages the URL ends in the vacancy id (/job/<slug>-<id>),
  -- which matched entity_id on every row carrying both (184,218 rows, Sep-Oct 2026), so it
  -- fills the gap there. Listing/organisation-page events carry no id in the URL; the next
  -- pipeline step (04_recover_ga4_entity_ids.sql) matches those on their vacancy details.
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
  -- Same window as the DELETE above; prunes to the source's last 7 partitions. NULL
  -- event_dates fail the comparison, so nothing lands in t04's NULL partition (which
  -- the DELETE never matches).
  WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
);
