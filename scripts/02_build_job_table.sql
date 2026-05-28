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
    external_id, title, organization_id, organization_name,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen
  FROM `site-monitoring-421401.JPD.t01_feed_ats`
  UNION ALL
  SELECT
    'Scrape',
    external_id, title, organization_id, organization_name,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen
  FROM `site-monitoring-421401.JPD.t01_feed_scrape`
  UNION ALL
  SELECT
    'Civil Service',
    external_id, title,
    jobiqo_org_id AS organization_id,
    CAST(NULL AS STRING) AS organization_name,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen
  FROM `site-monitoring-421401.JPD.t01_feed_civil_service`
  UNION ALL
  SELECT
    'Backfill',
    external_id, title, organization_id, organization_name,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen
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
    date_posted, date_end, locations, last_seen
  FROM `site-monitoring-421401.JPD.t01_feed_appcast`
),

-- ---------------------------------------------------------------------------
-- 4. Latest poll timestamp per feed. Used for is_live computation.
-- ---------------------------------------------------------------------------
max_seen AS (
  SELECT
    (SELECT MAX(last_seen) FROM `site-monitoring-421401.JPD.t01_feed_appcast`)       AS appcast_max,
    (SELECT MAX(last_seen) FROM `site-monitoring-421401.JPD.t01_feed_ats`)           AS ats_max,
    (SELECT MAX(last_seen) FROM `site-monitoring-421401.JPD.t01_feed_scrape`)        AS scrape_max,
    (SELECT MAX(last_seen) FROM `site-monitoring-421401.JPD.t01_feed_civil_service`) AS cs_max,
    (SELECT MAX(last_seen) FROM `site-monitoring-421401.JPD.t01_feed_backfill`)      AS backfill_max
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
    -- Source fields
    s.title                  AS source_title,
    s.organization_id        AS source_org_id,
    s.organization_name      AS source_org_name,
    s.occupation             AS source_occupation,
    s.category               AS source_category,
    s.working_pattern        AS source_working_pattern,
    s.salary_min, s.salary_max, s.salary_exact,
    s.salary_free_text, s.salary_type, s.salary_currency,
    s.start_date             AS source_start_date,
    s.close_date             AS source_close_date,
    s.last_seen              AS source_last_seen
  FROM appcast a
  FULL OUTER JOIN source_rows s USING (external_id)
),

joined_with_max AS (
  SELECT j.*, m.*
  FROM joined j
  CROSS JOIN max_seen m
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

  -- organization_id: Appcast ▸ source (CS jobiqo_org_id already aliased to organization_id)
  COALESCE(appcast_org_id, source_org_id) AS organization_id,
  CASE
    WHEN appcast_org_id IS NOT NULL THEN 'Appcast'
    WHEN source_org_id  IS NOT NULL THEN source_feed
    ELSE NULL
  END AS organization_id_source,

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

  -- is_live: row's last_seen matches the most recent poll of any feed it
  -- appears in. COALESCE to FALSE so a NULL side (matched-row's missing feed)
  -- can't poison the OR.
  (
    COALESCE(
      source_last_seen = CASE source_feed
        WHEN 'ATS'           THEN ats_max
        WHEN 'Scrape'        THEN scrape_max
        WHEN 'Civil Service' THEN cs_max
        WHEN 'Backfill'      THEN backfill_max
      END,
      FALSE
    )
    OR
    COALESCE(appcast_last_seen = appcast_max, FALSE)
  ) AS is_live,

  -- locations: Appcast only. Source-only rows leave this NULL — once Jobiqo
  -- imports the vacancy and Appcast picks it up, locations self-heal.
  appcast_locations AS locations

FROM joined_with_max
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
