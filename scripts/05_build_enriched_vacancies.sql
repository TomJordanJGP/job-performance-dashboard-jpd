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
--   * t02's _source provenance columns are NOT carried into t05 — they describe
--     t02's internal source-of-truth decisions, which would be confusing
--     alongside the separate t02-vs-t04 hybrid logic above. Query t02 directly
--     if provenance is needed.
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

  -- industry: 3-tier recovery so orphan events (no t02 row) still get a
  -- value where possible.
  --   1) j.industry — fully populated from t02 for matched events (which
  --      already does its own ID→name→org_type cascade against t04_organisations)
  --   2) orgs_event_id.industry — orphan events: lookup by GA4 organization_id
  --   3) orgs_event_name.industry — orphan events: lookup by GA4 organization_name
  COALESCE(
    j.industry,
    orgs_event_id.industry,
    orgs_event_name.industry
  )                                                                AS industry,

  -- uk_region: matched events get it directly from t02 (which already does
  -- its own postcode→city→HQ cascade). Orphan events recover via the same
  -- HQ-postcode chain that the industry COALESCE uses — the GA4 event's
  -- organization_id (then name) → t04_organisations.postcode → t04_postcodes.
  -- The "(pseudo) X" cleanup is applied in the same way as t02.
  COALESCE(
    j.uk_region,
    IF(orgs_event_id_region.region_name LIKE '(pseudo) %',
       orgs_event_id_region.country_name, orgs_event_id_region.region_name),
    IF(orgs_event_name_region.region_name LIKE '(pseudo) %',
       orgs_event_name_region.country_name, orgs_event_name_region.region_name)
  )                                                                AS uk_region,

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
  e.site,
  e.importer_ID

FROM `site-monitoring-421401.JPD.t04_vacancy_events` AS e
-- Dedup t02 to ONE row per entity_id before the event join. t02 is keyed on
-- external_id, so a re-listed vacancy can carry one entity_id across several
-- external_ids; without this, every GA4 event for that entity_id fans out across
-- those rows and over-counts clicks/applies. Prefer the live row, then the
-- latest by end/start date (external_id as a final deterministic tiebreak).
LEFT JOIN (
  SELECT * EXCEPT(_rn) FROM (
    SELECT *, ROW_NUMBER() OVER (
      PARTITION BY entity_id
      ORDER BY is_live DESC, end_date DESC, start_date DESC, external_id
    ) AS _rn
    FROM `site-monitoring-421401.JPD.t02_job_table`
    WHERE entity_id IS NOT NULL
  )
  WHERE _rn = 1
) AS j
  ON CAST(e.entity_id AS STRING) = j.entity_id
-- Industry recovery for orphan events (rows where j.entity_id IS NULL).
-- Tier 2: match GA4 event's organization_id to t04_organisations.
LEFT JOIN (
  SELECT organization_id, ANY_VALUE(industry) AS industry
  FROM `site-monitoring-421401.JPD.t04_organisations`
  WHERE organization_id IS NOT NULL
  GROUP BY organization_id
) AS orgs_event_id
  ON CAST(e.organization_id AS STRING) = orgs_event_id.organization_id
-- Tier 3: case-insensitive name match. Same pre-aggregation as t02 so
-- duplicate-name rows in the source don't multiply event rows.
LEFT JOIN (
  SELECT
    LOWER(TRIM(organisation_name)) AS name_key,
    ANY_VALUE(industry) AS industry
  FROM `site-monitoring-421401.JPD.t04_organisations`
  WHERE organisation_name IS NOT NULL
  GROUP BY name_key
) AS orgs_event_name
  ON LOWER(TRIM(e.organization_name)) = orgs_event_name.name_key
-- Region recovery for orphan events: same chain as industry, but pulling
-- region_name + country_name from t04_postcodes (via the org's HQ postcode).
LEFT JOIN (
  SELECT o.organization_id,
         ANY_VALUE(p.region_name)  AS region_name,
         ANY_VALUE(p.country_name) AS country_name
  FROM `site-monitoring-421401.JPD.t04_organisations` o
  LEFT JOIN `site-monitoring-421401.JPD.t04_postcodes` p
    ON UPPER(TRIM(o.postcode)) = p.postcode
  WHERE o.postcode IS NOT NULL AND o.organization_id IS NOT NULL
  GROUP BY o.organization_id
) AS orgs_event_id_region
  ON CAST(e.organization_id AS STRING) = orgs_event_id_region.organization_id
LEFT JOIN (
  SELECT
    LOWER(TRIM(o.organisation_name)) AS name_key,
    ANY_VALUE(p.region_name)  AS region_name,
    ANY_VALUE(p.country_name) AS country_name
  FROM `site-monitoring-421401.JPD.t04_organisations` o
  LEFT JOIN `site-monitoring-421401.JPD.t04_postcodes` p
    ON UPPER(TRIM(o.postcode)) = p.postcode
  WHERE o.organisation_name IS NOT NULL AND o.postcode IS NOT NULL
  GROUP BY name_key
) AS orgs_event_name_region
  ON LOWER(TRIM(e.organization_name)) = orgs_event_name_region.name_key

-- Exclude known test/demo vacancies. These entity_ids (Test.inc, Google, Dropbox,
-- GM, Exxon, stripe test, etc.) exist only as orphan GA4 events — no t02 row — so
-- dropping them here removes them from t05 + t06 durably. A DELETE on t04 would be
-- re-pulled by the next GA4 sync; this filter survives every rebuild.
WHERE CAST(e.entity_id AS STRING) NOT IN
  ('20', '23', '28', '29', '40', '62', '65', '77')

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
  j.industry,
  j.uk_region,
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
  CAST(NULL AS STRING)        AS site,
  CAST(NULL AS INT64)         AS importer_ID

FROM `site-monitoring-421401.JPD.t02_job_table` AS j
LEFT JOIN (
  SELECT DISTINCT CAST(entity_id AS STRING) AS entity_id_str
  FROM `site-monitoring-421401.JPD.t04_vacancy_events`
  WHERE entity_id IS NOT NULL
) AS has_events
  ON j.entity_id = has_events.entity_id_str
WHERE has_events.entity_id_str IS NULL;
