-- scripts/02_build_job_table.sql
-- Build JPD.t02_job_table — Silver unified vacancy table from Bronze.
--
-- One row per external_id. Reads the CURRENT STATE of the 5 Bronze tables
-- (t01_feed_*) — Bronze is already an upsert/current-state model, so no
-- "latest snapshot" logic needed here.
--
-- Three row segments:
--   - matched      : in Appcast AND in one source feed   (~3,856)
--   - Appcast-only : in Appcast, no source feed          (~2)
--   - source-only  : in a source feed, not yet in Appcast (~26 — entity_id NULL until Jobiqo registers it)
--
-- Source-feed priority for defensive dedupe (feeds are mutually exclusive
-- by design — this only fires if two feeds ever broadcast the same id):
--     ATS > Scrape > Civil Service > Backfill
-- A duplicate detection query is included at the bottom (commented).
--
-- Run:  bq query --use_legacy_sql=false < scripts/02_build_job_table.sql

-- Sentence-case + acronym allowlist. "Social work", "Ks2 teacher", "IT".
-- Applied to single-value categorical text (category, occupation,
-- workflow_state, salary_unit).
-- Add to the IN-list as new acronyms surface.
CREATE TEMP FUNCTION smart_case(s STRING) AS (
  CASE
    WHEN s IS NULL OR LENGTH(s) = 0 THEN s
    WHEN UPPER(s) IN ('IT', 'HR', 'GIS', 'SEO') THEN UPPER(s)
    ELSE CONCAT(UPPER(SUBSTR(s, 1, 1)), LOWER(SUBSTR(s, 2)))
  END
);

-- Compound-value helper. Source feeds emit "Full time | part time",
-- "Part time,flexible,full time,weekends" etc. Splits on '|' or ',',
-- smart_case each term, rejoins with ' | ' for a uniform display format.
CREATE TEMP FUNCTION smart_case_compound(s STRING) AS (
  IF(s IS NULL OR LENGTH(s) = 0, s, (
    SELECT STRING_AGG(smart_case(TRIM(part)), ' | ' ORDER BY pos)
    FROM UNNEST(SPLIT(REGEXP_REPLACE(s, r'\s*[|,]\s*', '|'), '|')) AS part WITH OFFSET pos
    WHERE TRIM(part) != ''
  ))
);

CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t02_job_table` AS

WITH

-- ---------------------------------------------------------------------------
-- 1. Stack the 4 source feeds. Each row carries its source_feed label.
--    Civil Service has no organization_name and uses jobiqo_org_id in place
--    of organization_id.
-- ---------------------------------------------------------------------------
source_union AS (
  SELECT
    'ATS' AS source_feed,
    external_id, title, organization_id, organization_name, organization_type,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen,
    jgp_external_vacancy_id
  FROM `site-monitoring-421401.JPD.t01_feed_ats`
  UNION ALL
  SELECT
    'Scrape',
    external_id, title, organization_id, organization_name, organization_type,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen,
    jgp_external_vacancy_id
  FROM `site-monitoring-421401.JPD.t01_feed_scrape`
  UNION ALL
  SELECT
    'Civil Service',
    external_id, title,
    jobiqo_org_id AS organization_id,
    CAST(NULL AS STRING) AS organization_name,
    CAST(NULL AS STRING) AS organization_type,  -- CS feed has no org_type column
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen,
    jgp_external_vacancy_id
  FROM `site-monitoring-421401.JPD.t01_feed_civil_service`
  UNION ALL
  SELECT
    'Backfill',
    external_id, title, organization_id, organization_name, organization_type,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen,
    jgp_external_vacancy_id
  FROM `site-monitoring-421401.JPD.t01_feed_backfill`
),

-- ---------------------------------------------------------------------------
-- 2. Pick ONE source row per external_id via priority. Mutually exclusive by
--    design, so rn=1 will be the only row 99.9% of the time.
-- ---------------------------------------------------------------------------
source_rows AS (
  SELECT * EXCEPT(rn)
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (
        PARTITION BY external_id
        ORDER BY CASE source_feed
          WHEN 'ATS' THEN 1
          WHEN 'Scrape' THEN 2
          WHEN 'Civil Service' THEN 3
          WHEN 'Backfill' THEN 4
        END
      ) AS rn
    FROM source_union
    WHERE external_id IS NOT NULL
  )
  WHERE rn = 1
),

-- ---------------------------------------------------------------------------
-- 3. Appcast (registry overlay). Site-created jobs (entity_id present,
--    external_id NULL — Jobiqo hasn't hashed them yet) are kept; they appear
--    in t02 as Appcast-only rows with external_id NULL.
-- ---------------------------------------------------------------------------
appcast AS (
  SELECT
    external_id, entity_id, title, company, organization_id,
    occupation, employment_type, workflow_state,
    date_posted, date_end, locations, last_seen,
    jgp_external_vacancy_id
  FROM `site-monitoring-421401.JPD.t01_feed_appcast`
),

-- ---------------------------------------------------------------------------
-- 4. Live-window threshold. A row counts as "live" if last_seen is within
--    the last 24 hours — robust to multi-cadence / ad-hoc polls that an
--    exact MAX(last_seen) comparison would mis-classify.
-- ---------------------------------------------------------------------------
live_window AS (
  SELECT TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR) AS threshold
),

-- ---------------------------------------------------------------------------
-- 5. FULL OUTER JOIN — captures all three segments. Carries both sides'
--    fields prefixed so the final SELECT can apply COALESCE rules.
-- ---------------------------------------------------------------------------
joined AS (
  SELECT
    COALESCE(a.external_id, s.external_id) AS external_id,
    a.entity_id              AS appcast_entity_id,
    s.source_feed,
    -- Appcast fields
    a.title                  AS appcast_title,
    a.company                AS appcast_company,
    a.organization_id        AS appcast_org_id,
    a.occupation             AS appcast_occupation,
    a.employment_type        AS appcast_employment_type,
    a.workflow_state         AS appcast_workflow_state,
    a.date_posted            AS appcast_date_posted,
    a.date_end               AS appcast_date_end,
    a.locations              AS appcast_locations,
    a.last_seen              AS appcast_last_seen,
    a.jgp_external_vacancy_id AS appcast_jgp_id,
    -- Source fields
    s.title                  AS source_title,
    s.organization_id        AS source_org_id,
    s.organization_name      AS source_org_name,
    s.organization_type      AS source_org_type,
    s.occupation             AS source_occupation,
    s.category               AS source_category,
    s.working_pattern        AS source_working_pattern,
    s.salary_min, s.salary_max, s.salary_exact,
    s.salary_free_text, s.salary_type, s.salary_currency,
    s.start_date             AS source_start_date,
    s.close_date             AS source_close_date,
    s.last_seen              AS source_last_seen,
    s.jgp_external_vacancy_id AS source_jgp_id
  FROM appcast a
  FULL OUTER JOIN source_rows s USING (external_id)
),

joined_with_max AS (
  SELECT j.*, w.threshold AS live_threshold
  FROM joined j
  CROSS JOIN live_window w
)

-- ---------------------------------------------------------------------------
-- 6. Final SELECT — apply source-of-truth rules + provenance + is_live.
-- ---------------------------------------------------------------------------
SELECT
  external_id,
  appcast_entity_id AS entity_id,
  COALESCE(source_feed, 'Appcast') AS source_feed,

  -- title: Appcast ▸ source
  COALESCE(appcast_title, source_title) AS title,
  CASE
    WHEN appcast_title IS NOT NULL THEN 'Appcast'
    WHEN source_title  IS NOT NULL THEN source_feed
    ELSE NULL
  END AS title_source,

  -- organization_name: Appcast.company ▸ source.organization_name
  COALESCE(appcast_company, source_org_name) AS organization_name,
  CASE
    WHEN appcast_company IS NOT NULL THEN 'Appcast'
    WHEN source_org_name IS NOT NULL THEN source_feed
    ELSE NULL
  END AS organization_name_source,

  -- organization_id: source ▸ Appcast.  Source feeds are more reliable; Appcast
  -- produces some inconsistent company IDs (different IDs for the same logical
  -- org), so the source feed's org_id wins where present. Appcast.organization_id
  -- only fills in for site-created Appcast-only rows.
  COALESCE(source_org_id, appcast_org_id) AS organization_id,
  CASE
    WHEN source_org_id  IS NOT NULL THEN source_feed
    WHEN appcast_org_id IS NOT NULL THEN 'Appcast'
    ELSE NULL
  END AS organization_id_source,

  -- industry: 3-tier lookup against t04_organisations.
  --   1) ID match  — orgs_by_id.industry  (final organization_id → t04 row)
  --   2) Name match — orgs_by_name.industry (LOWER(TRIM(org_name)) → t04 row)
  --      Recovers orgs whose org_id is in a different namespace than t04 uses
  --      (e.g. Civil Service feed's gov.uk-side IDs vs Jobiqo's profile IDs).
  --   3) org_type fallback — strip Parent/Child/Standard from feed XML.
  --      Currently dormant: every org with a non-junk org_type is already in
  --      the lookup; kept as a safety net for future orgs.
  -- Name-match has 9 known disagreement cases in the lookup (e.g. "National
  -- Crime Agency" with two rows, one Local Government, one Civil Service).
  -- Risk is low because those orgs match by ID first; name only fires when
  -- ID misses, and arbitrary pick is acceptable.
  COALESCE(
    orgs_by_id.industry,
    orgs_by_name.industry,
    CASE
      WHEN source_org_type IS NULL THEN NULL
      WHEN LOWER(source_org_type) IN ('parent', 'child', 'standard') THEN NULL
      ELSE source_org_type
    END
  ) AS industry,

  -- occupation: narrow classification, source feed only (no Appcast equivalent)
  smart_case(source_occupation) AS occupation,

  -- category: broad classification. Source feeds use <category>; Appcast uses
  -- <occupation> for the same concept. Source naming wins where present, fall
  -- back to Appcast.occupation for Appcast-only (site-created) rows.
  smart_case(COALESCE(source_category, appcast_occupation)) AS category,
  CASE
    WHEN source_category    IS NOT NULL THEN source_feed
    WHEN appcast_occupation IS NOT NULL THEN 'Appcast'
    ELSE NULL
  END AS category_source,

  -- employment_type: Appcast ▸ source.working_pattern. Multi-value safe.
  smart_case_compound(COALESCE(appcast_employment_type, source_working_pattern)) AS employment_type,
  CASE
    WHEN appcast_employment_type IS NOT NULL THEN 'Appcast'
    WHEN source_working_pattern  IS NOT NULL THEN source_feed
    ELSE NULL
  END AS employment_type_source,

  -- workflow_state: Appcast ▸ 'published'
  smart_case(COALESCE(appcast_workflow_state, 'published')) AS workflow_state,
  CASE
    WHEN appcast_workflow_state IS NOT NULL THEN 'Appcast'
    ELSE 'default'
  END AS workflow_state_source,

  -- start_date: source.start_date ▸ Appcast.date_posted
  COALESCE(source_start_date, appcast_date_posted) AS start_date,
  CASE
    WHEN source_start_date   IS NOT NULL THEN source_feed
    WHEN appcast_date_posted IS NOT NULL THEN 'Appcast'
    ELSE NULL
  END AS start_date_source,

  -- end_date: source.close_date ▸ Appcast.date_end
  COALESCE(source_close_date, appcast_date_end) AS end_date,
  CASE
    WHEN source_close_date IS NOT NULL THEN source_feed
    WHEN appcast_date_end  IS NOT NULL THEN 'Appcast'
    ELSE NULL
  END AS end_date_source,

  -- Salary: source-only fields. No _source col — source_feed already tells us.
  salary_min      AS min_salary,
  salary_max      AS max_salary,
  salary_exact    AS salary_exact,
  salary_free_text,
  smart_case(salary_type) AS salary_unit,
  salary_currency AS currency_code,

  -- jgp_external_vacancy_id: JGP-side identifier for joining future data.
  -- Source feed wins (for ATS this is the live old_vacancy_id or historic
  -- jgp value); Appcast as fallback for site-created rows (almost always NULL).
  COALESCE(source_jgp_id, appcast_jgp_id) AS jgp_external_vacancy_id,

  -- is_live: TRUE if the row appears in either feed within the last 24 hours
  -- of CURRENT_TIMESTAMP(). Robust to multi-cadence/ad-hoc polls that an
  -- exact MAX(last_seen) comparison would mis-classify when a single late
  -- poll pulls MAX away from the main poll cluster.
  (
    COALESCE(source_last_seen  >= live_threshold, FALSE)
    OR
    COALESCE(appcast_last_seen >= live_threshold, FALSE)
  ) AS is_live,

  -- locations: Appcast only. Source-only rows leave this NULL — once Jobiqo
  -- imports the vacancy and Appcast picks it up, locations self-heal.
  appcast_locations AS locations

FROM joined_with_max
-- Industry lookup tier 1: ID match. Uses the final (source ▸ Appcast) org_id
-- — matches the COALESCE in the SELECT above.
LEFT JOIN `site-monitoring-421401.JPD.t04_organisations` AS orgs_by_id
  ON COALESCE(source_org_id, appcast_org_id) = orgs_by_id.organization_id
-- Industry lookup tier 2: case-insensitive name match. Only takes effect when
-- ID match misses, via the COALESCE in the SELECT. Pre-aggregates the lookup
-- (ANY_VALUE) so duplicate-name rows in the source don't multiply t02 rows;
-- the 9 known disagreement cases (e.g. National Crime Agency) resolve to one
-- arbitrary industry — low risk because those orgs match on ID first.
LEFT JOIN (
  SELECT
    LOWER(TRIM(organisation_name)) AS name_key,
    ANY_VALUE(industry) AS industry
  FROM `site-monitoring-421401.JPD.t04_organisations`
  WHERE organisation_name IS NOT NULL
  GROUP BY name_key
) AS orgs_by_name
  ON LOWER(TRIM(COALESCE(appcast_company, source_org_name))) = orgs_by_name.name_key
;

-- ---------------------------------------------------------------------------
-- Optional duplicate-detection query — run separately if you want to confirm
-- the "feeds are mutually exclusive" assumption is holding. Should return 0
-- rows. If it returns anything, the SQL above already deduped via priority
-- (ATS > Scrape > CS > Backfill); this query just tells you it happened.
--
-- SELECT external_id, ARRAY_AGG(source_feed ORDER BY source_feed) AS feeds
-- FROM (
--   SELECT 'ATS' AS source_feed, external_id FROM `site-monitoring-421401.JPD.t01_feed_ats` WHERE external_id IS NOT NULL
--   UNION ALL
--   SELECT 'Scrape', external_id FROM `site-monitoring-421401.JPD.t01_feed_scrape` WHERE external_id IS NOT NULL
--   UNION ALL
--   SELECT 'Civil Service', external_id FROM `site-monitoring-421401.JPD.t01_feed_civil_service` WHERE external_id IS NOT NULL
--   UNION ALL
--   SELECT 'Backfill', external_id FROM `site-monitoring-421401.JPD.t01_feed_backfill` WHERE external_id IS NOT NULL
-- )
-- GROUP BY external_id
-- HAVING COUNT(*) > 1;
