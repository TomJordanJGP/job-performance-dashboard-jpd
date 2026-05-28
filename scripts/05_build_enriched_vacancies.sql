-- Build t05_enriched_vacancies — Gold event-grain table.
--
-- Shape: one row per GA4 event (from t04_vacancy_events) enriched with vacancy
-- attributes (from t02_job_table), plus synthetic 'metadata_only' rows for any
-- t02 vacancy that has no GA4 events yet (so vacancies with zero traffic still
-- appear in vacancy-level reports like salary benchmarks).
--
-- Three sources UNION ALL'd:
--   Part 1 — t04 LEFT JOIN t02 on entity_id. Covers:
--            * matched events (vacancy in both tables) — full enrichment
--            * orphan events (entity_id has events but no t02 row) — vacancy
--              fields NULL; events still count toward totals (matches prod
--              behaviour + GA4 truth)
--   Part 2 — t02 vacancies with no matching events. Synthetic row with
--            event_name='metadata_only', Events=0. event_date_dt set from
--            t02.start_date so partitioning works (NULL allowed for the few
--            t02 rows with no start_date).
--
-- Field source rules (locked in 2026-05-28):
--   * Vacancy attributes (title, organization_name, organization_id, category):
--     t02 is master, t04 is fallback. COALESCE(t02.X, t04.X).
--   * category in t05 = COALESCE(t02.category, t04.occupations) — t02's broad
--     category field maps to GA4's 'occupations' field as fallback.
--   * t02-only fields (salary, locations, dates, source_feed, etc.): no
--     fallback — NULL where t02 has no row (orphan events).
--   * GA4-only fields (device, browser, campaign, page_*, etc.): NULL on
--     metadata_only rows.
--   * t02's _source provenance columns pass through unchanged. They describe
--     t02's internal field-source decisions, NOT the t02-vs-t04 hybrid at t05.
--
-- Partition: event_date_dt DATE (matches t04 partition strategy).
-- Cluster:   entity_id, organization_id.

CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t05_enriched_vacancies`
PARTITION BY event_date_dt
CLUSTER BY entity_id, organization_id
AS

-- =============================================================================
-- Part 1: All GA4 events enriched with t02 vacancy attributes.
--         LEFT JOIN keeps orphan events (entity_id present in t04 but not t02).
-- =============================================================================
SELECT
  -- Event facts (from t04)
  e.event_name,
  e.event_date,
  e.event_date_dt,
  e.hour_of_day,
  e.Events,

  -- Identity
  CAST(e.entity_id AS STRING)        AS entity_id,
  j.external_id,

  -- Hybrid fields: t02 wins when present, else fall back to t04
  COALESCE(j.title, e.title)                                       AS title,
  COALESCE(j.organization_name, e.organization_name)               AS organization_name,
  COALESCE(j.organization_id, CAST(e.organization_id AS STRING))   AS organization_id,
  COALESCE(j.category, e.occupations)                              AS category,

  -- t02-only fields (NULL for orphan events)
  j.source_feed,
  j.occupation,
  j.locations,
  j.employment_type,
  j.workflow_state,
  j.start_date,
  j.end_date,
  j.min_salary,
  j.max_salary,
  j.salary_exact,
  j.salary_free_text,
  j.salary_unit,
  j.currency_code,
  j.jgp_external_vacancy_id,
  j.is_live,

  -- t02 provenance (pass-through from Phase 3 source-of-truth rules)
  j.title_source,
  j.organization_name_source,
  j.organization_id_source,
  j.category_source,
  j.employment_type_source,
  j.workflow_state_source,
  j.start_date_source,
  j.end_date_source,

  -- GA4-only context
  e.regions       AS ga4_location,
  e.page_referrer,
  e.page_location,
  e.upgrades,
  e.device,
  e.operating_system,
  e.browser,
  e.campaign,
  e.medium,
  e.source,
  e.site

FROM `site-monitoring-421401.JPD.t04_vacancy_events` AS e
LEFT JOIN `site-monitoring-421401.JPD.t02_job_table` AS j
  ON CAST(e.entity_id AS STRING) = j.entity_id

UNION ALL

-- =============================================================================
-- Part 2: Metadata-only — t02 vacancies with no matching GA4 events.
--         Synthetic event_name='metadata_only', Events=0, event_date_dt from
--         t02.start_date (NULL allowed). Filter excluded via LEFT JOIN ... NULL.
--         Handles both:
--           * t02 rows with entity_id but no events
--           * t02 rows with NULL entity_id (source-only vacancies, can't join)
-- =============================================================================
SELECT
  'metadata_only'             AS event_name,
  CAST(NULL AS STRING)        AS event_date,
  DATE(j.start_date)          AS event_date_dt,
  CAST(NULL AS STRING)        AS hour_of_day,
  0                           AS Events,

  j.entity_id,
  j.external_id,

  j.title,
  j.organization_name,
  j.organization_id,
  j.category,

  j.source_feed,
  j.occupation,
  j.locations,
  j.employment_type,
  j.workflow_state,
  j.start_date,
  j.end_date,
  j.min_salary,
  j.max_salary,
  j.salary_exact,
  j.salary_free_text,
  j.salary_unit,
  j.currency_code,
  j.jgp_external_vacancy_id,
  j.is_live,

  j.title_source,
  j.organization_name_source,
  j.organization_id_source,
  j.category_source,
  j.employment_type_source,
  j.workflow_state_source,
  j.start_date_source,
  j.end_date_source,

  CAST(NULL AS STRING)        AS ga4_location,
  CAST(NULL AS STRING)        AS page_referrer,
  CAST(NULL AS STRING)        AS page_location,
  CAST(NULL AS STRING)        AS upgrades,
  CAST(NULL AS STRING)        AS device,
  CAST(NULL AS STRING)        AS operating_system,
  CAST(NULL AS STRING)        AS browser,
  CAST(NULL AS STRING)        AS campaign,
  CAST(NULL AS STRING)        AS medium,
  CAST(NULL AS STRING)        AS source,
  CAST(NULL AS STRING)        AS site

FROM `site-monitoring-421401.JPD.t02_job_table` AS j
LEFT JOIN (
  SELECT DISTINCT CAST(entity_id AS STRING) AS entity_id_str
  FROM `site-monitoring-421401.JPD.t04_vacancy_events`
  WHERE entity_id IS NOT NULL
) AS has_events
  ON j.entity_id = has_events.entity_id_str
WHERE has_events.entity_id_str IS NULL;
