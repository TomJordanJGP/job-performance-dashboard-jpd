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
-- 1. Stack the source feeds. Each row carries its source_feed label.
--    GENERATED — the source_union body and the source-priority CASE below are
--    templated from t00_feed_registry (feed_kind='source', ordered by priority)
--    by scripts/build_job_table.py. Do NOT hand-edit the generated blocks; add
--    a feed via a registry INSERT and rebuild. Civil Service maps jobiqo_org_id
--    -> organization_id and has no organization_name/organization_type (NULL'd).
-- ---------------------------------------------------------------------------
source_union AS (
{{SOURCE_UNION}}
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
{{SOURCE_PRIORITY_CASE}}
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
-- 4b. Region-resolution helpers — Tier 2 (city/town → region) and Tier 3
--     (org HQ postcode → region). Tier 1 is just a direct join on t04_postcodes
--     so doesn't need a CTE. Names are case-normalised and disambiguated by
--     picking the dominant region per name (the city/town name with the most
--     postcodes wins where the same name appears in multiple regions — e.g.
--     "Newport" exists in Wales and Isle of Wight; Wales wins by row count).
--     All non-English ONSPD rows label `region_name` as "(pseudo) Scotland"
--     etc — the cleanup pattern is applied at the SELECT, not here.
-- ---------------------------------------------------------------------------
city_to_region AS (
  WITH agg AS (
    SELECT
      LOWER(TRIM(city_name)) AS name_key,
      region_name,
      country_name,
      COUNT(*) AS n
    FROM `site-monitoring-421401.JPD.t04_postcodes`
    WHERE city_name IS NOT NULL AND region_name IS NOT NULL
    GROUP BY name_key, region_name, country_name
  )
  SELECT name_key, region_name, country_name FROM (
    SELECT name_key, region_name, country_name,
           ROW_NUMBER() OVER (PARTITION BY name_key ORDER BY n DESC) AS rn
    FROM agg
  )
  WHERE rn = 1
),
town_to_region AS (
  WITH agg AS (
    SELECT
      LOWER(TRIM(town_name)) AS name_key,
      region_name,
      country_name,
      COUNT(*) AS n
    FROM `site-monitoring-421401.JPD.t04_postcodes`
    WHERE town_name IS NOT NULL AND region_name IS NOT NULL
    GROUP BY name_key, region_name, country_name
  )
  SELECT name_key, region_name, country_name FROM (
    SELECT name_key, region_name, country_name,
           ROW_NUMBER() OVER (PARTITION BY name_key ORDER BY n DESC) AS rn
    FROM agg
  )
  WHERE rn = 1
),
hq_to_region AS (
  -- One region per organization_id. GROUP BY guards against the handful of
  -- duplicate organization_id rows in t04_organisations multiplying t02 rows
  -- where this is joined on org_id (t3_hq / t3_ss_hq).
  SELECT
    o.organization_id,
    ANY_VALUE(p.region_name)  AS region_name,
    ANY_VALUE(p.country_name) AS country_name
  FROM `site-monitoring-421401.JPD.t04_organisations` o
  LEFT JOIN `site-monitoring-421401.JPD.t04_postcodes` p
    ON UPPER(TRIM(o.postcode)) = p.postcode
  WHERE o.postcode IS NOT NULL
  GROUP BY o.organization_id
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

  -- uk_region: 3-tier cascade against ONS reference data (t04_postcodes +
  -- t04_organisations). Non-English ONSPD rows label region_name as
  -- "(pseudo) Scotland" etc — strip the prefix by falling back to
  -- country_name so output is clean (London / North West / Scotland / etc).
  --   Tier 1: vacancy's postcode → t04_postcodes.region_name
  --   Tier 2: vacancy's city → t04_postcodes.town_name then city_name
  --   Tier 3: vacancy's organization_id → t04_organisations.postcode → t04_postcodes
  COALESCE(
    IF(t1_postcode.region_name LIKE '(pseudo) %', t1_postcode.country_name, t1_postcode.region_name),
    IF(t2_town.region_name     LIKE '(pseudo) %', t2_town.country_name,     t2_town.region_name),
    IF(t2_city.region_name     LIKE '(pseudo) %', t2_city.country_name,     t2_city.region_name),
    IF(t3_hq.region_name       LIKE '(pseudo) %', t3_hq.country_name,       t3_hq.region_name)
  ) AS uk_region,

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
LEFT JOIN (
  SELECT organization_id, ANY_VALUE(industry) AS industry
  FROM `site-monitoring-421401.JPD.t04_organisations`
  WHERE organization_id IS NOT NULL
  GROUP BY organization_id
) AS orgs_by_id
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
-- Region tier 1: postcode match. Direct join — t04_postcodes is keyed on
-- postcode in the standard 'M1 1AA' form. Both sides UPPER/TRIM-normalised.
LEFT JOIN `site-monitoring-421401.JPD.t04_postcodes` AS t1_postcode
  ON UPPER(TRIM(appcast_locations[SAFE_OFFSET(0)].postcode)) = t1_postcode.postcode
-- Region tier 2a: BUASD (town) name match — more specific, tries first.
LEFT JOIN town_to_region AS t2_town
  ON LOWER(TRIM(appcast_locations[SAFE_OFFSET(0)].city)) = t2_town.name_key
-- Region tier 2b: BUA (broader agglomeration) name match — fallback if town misses.
LEFT JOIN city_to_region AS t2_city
  ON LOWER(TRIM(appcast_locations[SAFE_OFFSET(0)].city)) = t2_city.name_key
-- Region tier 3: HQ postcode chain. Final fallback when the vacancy itself
-- has no usable location data.
LEFT JOIN hq_to_region AS t3_hq
  ON COALESCE(source_org_id, appcast_org_id) = t3_hq.organization_id

UNION ALL

-- ---------------------------------------------------------------------------
-- 7. Self-service vacancies (4th t02 segment).  These are user-created jobs
--    that never enter the live feeds or Appcast XML — entity_id-keyed, no
--    external_id. Loaded one-off from manually_created_vacancies.csv into
--    t01_feed_selfservice.  Filtered to exclude entity_ids already present
--    in Appcast (the ~20 site-created Appcast-only rows) to avoid duplicate
--    t02 rows for the same vacancy.
-- ---------------------------------------------------------------------------
SELECT
  -- external_id: self-service jobs are hand-created and never receive a feed
  -- external_id. Synthesise a unique, collision-proof one (SS-<entity_id>) so they
  -- don't surface as external_id gaps in analysis. entity_id is non-null and unique
  -- for these rows, and this segment never joins on external_id, so no merge risk.
  CONCAT('SS-', ss.entity_id)                           AS external_id,
  ss.entity_id,
  'Self-service'                                        AS source_feed,

  ss.title,
  IF(ss.title IS NOT NULL, 'Self-service', NULL)        AS title_source,

  ss.organization_name,
  IF(ss.organization_name IS NOT NULL, 'Self-service', NULL) AS organization_name_source,

  ss.organization_id,
  IF(ss.organization_id IS NOT NULL, 'Self-service', NULL)   AS organization_id_source,

  -- industry: same 3-tier lookup as main rows. CSV's "Employer type (Industry)"
  -- is carried as ss.organization_type and is 92% populated with the canonical
  -- 9 industries — it almost always wins via the tier-3 leg even if tiers 1+2
  -- fail.
  COALESCE(
    orgs_ss_by_id.industry,
    orgs_ss_by_name.industry,
    CASE
      WHEN ss.organization_type IS NULL THEN NULL
      WHEN LOWER(ss.organization_type) IN ('parent', 'child', 'standard') THEN NULL
      ELSE ss.organization_type
    END
  )                                                     AS industry,

  -- uk_region: self-service rows have no structured postcode/city, only a flat
  -- string in formatted_address — so Tiers 1 and 2 can't fire here. Only
  -- Tier 3 (HQ postcode → region) applies.
  IF(t3_ss_hq.region_name LIKE '(pseudo) %', t3_ss_hq.country_name, t3_ss_hq.region_name)
                                                        AS uk_region,

  smart_case(ss.occupation)                             AS occupation,

  CAST(NULL AS STRING)                                  AS category,
  CAST(NULL AS STRING)                                  AS category_source,

  smart_case_compound(ss.working_pattern)               AS employment_type,
  IF(ss.working_pattern IS NOT NULL, 'Self-service', NULL) AS employment_type_source,

  smart_case(ss.workflow_state)                         AS workflow_state,
  'Self-service'                                        AS workflow_state_source,

  ss.start_date,
  IF(ss.start_date IS NOT NULL, 'Self-service', NULL)   AS start_date_source,

  ss.close_date                                         AS end_date,
  IF(ss.close_date IS NOT NULL, 'Self-service', NULL)   AS end_date_source,

  ss.salary_min                                         AS min_salary,
  ss.salary_max                                         AS max_salary,
  ss.salary_exact                                       AS salary_exact,
  ss.salary_free_text                                   AS salary_free_text,
  smart_case(ss.salary_type)                            AS salary_unit,
  ss.salary_currency                                    AS currency_code,

  ss.jgp_external_vacancy_id,

  -- Self-service vacancies don't appear in feed polls, so is_live is always
  -- FALSE here. (If one ever gets published, it'll show up via a source feed
  -- AND get the live flag through the main segments.)
  FALSE                                                 AS is_live,

  -- locations: CSV is a flat string like "London, UK". Wrap it in a single
  -- STRUCT in the formatted_address slot so the column type matches the
  -- main segments. Phase 6 region resolution will parse this centrally.
  IF(
    ss.locations IS NULL OR LENGTH(TRIM(ss.locations)) = 0,
    NULL,
    [STRUCT(
      CAST(NULL AS STRING) AS country,
      CAST(NULL AS STRING) AS region,
      CAST(NULL AS STRING) AS postcode,
      CAST(NULL AS STRING) AS city,
      CAST(NULL AS STRING) AS street,
      ss.locations         AS formatted_address
    )]
  )                                                     AS locations

FROM `site-monitoring-421401.JPD.t01_feed_selfservice` AS ss
-- Same 3-tier industry lookup the main segments use
LEFT JOIN (
  SELECT organization_id, ANY_VALUE(industry) AS industry
  FROM `site-monitoring-421401.JPD.t04_organisations`
  WHERE organization_id IS NOT NULL
  GROUP BY organization_id
) AS orgs_ss_by_id
  ON ss.organization_id = orgs_ss_by_id.organization_id
LEFT JOIN (
  SELECT
    LOWER(TRIM(organisation_name)) AS name_key,
    ANY_VALUE(industry) AS industry
  FROM `site-monitoring-421401.JPD.t04_organisations`
  WHERE organisation_name IS NOT NULL
  GROUP BY name_key
) AS orgs_ss_by_name
  ON LOWER(TRIM(ss.organization_name)) = orgs_ss_by_name.name_key
-- Region tier 3: HQ postcode chain for self-service rows.
LEFT JOIN hq_to_region AS t3_ss_hq
  ON ss.organization_id = t3_ss_hq.organization_id
-- Filter out entity_ids already in Appcast (the ~20 Appcast-only overlaps).
-- This keeps the existing Appcast-only segment unchanged for those rows.
LEFT JOIN (
  SELECT DISTINCT entity_id
  FROM `site-monitoring-421401.JPD.t01_feed_appcast`
  WHERE entity_id IS NOT NULL
) AS already_in_appcast
  ON ss.entity_id = already_in_appcast.entity_id
WHERE already_in_appcast.entity_id IS NULL
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
