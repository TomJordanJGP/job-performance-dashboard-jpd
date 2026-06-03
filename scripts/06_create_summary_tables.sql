-- scripts/06_create_summary_tables.sql
-- Gold pre-aggregated tables the dashboard reads (via data/loader.py).
-- Run as ONE BigQuery script (multi-statement) in EU:
--     client.query(open('scripts/06_create_summary_tables.sql').read()).result()
--
-- Builds four tables from t05_enriched_vacancies (+ t04 reference tables):
--   t06_summary_vacancy         — one row per entity_id
--   t06_summary_vacancy_region  — one row per (vacancy x located region)  [MULTI-REGION]
--   t06_summary_daily_totals    — one row per day (GA4 + GSC + active counts)
--   t06_summary_media           — one row per (vacancy x source x medium x campaign)
--
-- Decisions (2026-06-02):
--   * clicks/applies = SUM(Events) by event_name (t05 is event-grain with an
--     Events count column — NOT one physical row per event).
--   * Regions: full multi-region explosion — every entry in t05.locations is
--     resolved to a UK region (postcode -> town -> city cascade, same as t02),
--     so a vacancy spanning N regions yields N region rows and uk_regions is a
--     pipe-joined list.
--   * GSC: read from the pre-aggregated t04_gsc_daily (incrementally synced —
--     see 04_create_and_backfill_gsc.sql / 04_sync_gsc_daily.sql).
--   * contract_type: NULL for now (not in t05; no view consumes it yet).
--   * importer_name: joined from t04_importers on the importer_ID t05 carries.

-- ===========================================================================
-- Shared TEMP table: resolve EVERY vacancy location to a UK region.
-- One row per (entity_id, location). uk_region is NULL where a location does
-- not resolve. Mirrors the t02 cascade: postcode -> BUASD town -> BUA city,
-- with the ONSPD "(pseudo) X" prefix stripped to the country name.
-- Drives both uk_regions (T1, distinct pipe-join) and the explosion (T2).
-- ===========================================================================
CREATE TEMP TABLE _vacancy_locations_resolved AS
WITH
town_to_region AS (
  WITH agg AS (
    SELECT LOWER(TRIM(town_name)) AS name_key, region_name, country_name, COUNT(*) AS n
    FROM `site-monitoring-421401.JPD.t04_postcodes`
    WHERE town_name IS NOT NULL AND region_name IS NOT NULL
    GROUP BY name_key, region_name, country_name
  )
  SELECT name_key, region_name, country_name FROM (
    SELECT name_key, region_name, country_name,
           ROW_NUMBER() OVER (PARTITION BY name_key ORDER BY n DESC) AS rn
    FROM agg
  ) WHERE rn = 1
),
city_to_region AS (
  WITH agg AS (
    SELECT LOWER(TRIM(city_name)) AS name_key, region_name, country_name, COUNT(*) AS n
    FROM `site-monitoring-421401.JPD.t04_postcodes`
    WHERE city_name IS NOT NULL AND region_name IS NOT NULL
    GROUP BY name_key, region_name, country_name
  )
  SELECT name_key, region_name, country_name FROM (
    SELECT name_key, region_name, country_name,
           ROW_NUMBER() OVER (PARTITION BY name_key ORDER BY n DESC) AS rn
    FROM agg
  ) WHERE rn = 1
),
-- locations is constant per vacancy; collapse to one array per entity_id first.
vac AS (
  SELECT entity_id, ANY_VALUE(locations) AS locations
  FROM `site-monitoring-421401.JPD.t05_enriched_vacancies`
  WHERE entity_id IS NOT NULL
  GROUP BY entity_id
)
SELECT
  vac.entity_id,
  COALESCE(
    IF(p.region_name  LIKE '(pseudo) %', p.country_name,  p.region_name),
    IF(tt.region_name LIKE '(pseudo) %', tt.country_name, tt.region_name),
    IF(ct.region_name LIKE '(pseudo) %', ct.country_name, ct.region_name)
  ) AS uk_region,
  loc.formatted_address AS raw_location,
  loc.city              AS town_city
FROM vac, UNNEST(vac.locations) AS loc
LEFT JOIN `site-monitoring-421401.JPD.t04_postcodes` p
  ON UPPER(TRIM(loc.postcode)) = p.postcode
LEFT JOIN town_to_region tt ON LOWER(TRIM(loc.city)) = tt.name_key
LEFT JOIN city_to_region ct ON LOWER(TRIM(loc.city)) = ct.name_key;


-- ===========================================================================
-- T1: t06_summary_vacancy — one row per entity_id.
-- Includes metadata_only vacancies (zero traffic) so salary/field analysis
-- still sees them. clicks/applies = SUM(Events) by event_name.
-- ===========================================================================
CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t06_summary_vacancy` AS
WITH agg AS (
  SELECT
    entity_id,
    MIN(event_date_dt) AS first_event_date,
    MAX(event_date_dt) AS last_event_date,
    SUM(IF(event_name = 'job_visit',       Events, 0)) AS clicks,
    SUM(IF(event_name = 'job_apply_start', Events, 0)) AS applies,
    ANY_VALUE(title)             AS title,
    ANY_VALUE(organization_name) AS organization_name,
    ANY_VALUE(uk_region)         AS primary_uk_region,
    ANY_VALUE(category)          AS occupational_fields,
    ANY_VALUE(importer_ID)       AS importer_ID,
    ANY_VALUE(workflow_state)    AS workflow_state,
    ANY_VALUE(upgrades)          AS upgrades,
    ANY_VALUE(start_date)        AS start_date,
    ANY_VALUE(end_date)          AS end_date,
    ANY_VALUE(category)          AS category,
    ANY_VALUE(employment_type)   AS employment_type,
    ANY_VALUE(external_id)       AS external_id,
    ANY_VALUE(min_salary)        AS min_salary,
    ANY_VALUE(max_salary)        AS max_salary,
    ANY_VALUE(currency_code)     AS currency_code,
    ANY_VALUE(salary_free_text)  AS salary_free_text,
    ANY_VALUE(salary_exact)      AS salary_exact,
    ANY_VALUE(salary_unit)       AS salary_unit,
    STRING_AGG(DISTINCT site, ' | ' ORDER BY site) AS sites
  FROM `site-monitoring-421401.JPD.t05_enriched_vacancies`
  WHERE event_name IN ('job_visit', 'job_apply_start', 'metadata_only')
    AND entity_id IS NOT NULL
  GROUP BY entity_id
),
regions AS (
  SELECT entity_id, STRING_AGG(DISTINCT uk_region, ' | ' ORDER BY uk_region) AS uk_regions
  FROM _vacancy_locations_resolved
  WHERE uk_region IS NOT NULL
  GROUP BY entity_id
)
SELECT
  a.entity_id AS entity_id_str,
  a.first_event_date,
  a.last_event_date,
  a.clicks,
  a.applies,
  a.title,
  a.organization_name,
  COALESCE(r.uk_regions, a.primary_uk_region) AS uk_regions,
  a.primary_uk_region,
  a.occupational_fields,
  a.importer_ID,
  imp.importer_name,
  a.workflow_state,
  a.upgrades,
  a.start_date,
  a.end_date,
  a.category,
  CAST(NULL AS STRING) AS contract_type,
  a.employment_type,
  a.external_id,
  a.min_salary,
  a.max_salary,
  a.currency_code,
  a.salary_free_text,
  a.salary_exact,
  a.salary_unit,
  a.sites
FROM agg a
LEFT JOIN regions r USING (entity_id)
LEFT JOIN `site-monitoring-421401.JPD.t04_importers` imp
  ON a.importer_ID = imp.importer_id;


-- ===========================================================================
-- T2: t06_summary_vacancy_region — one row per (vacancy x located region).
-- MULTI-REGION: each resolved location becomes a row carrying the FULL
-- click/apply counts (GA4 can't attribute an event to one location of a
-- multi-location vacancy — so regional totals intentionally exceed overall).
-- Vacancies with no resolved location fall back to a single primary-region row.
-- ===========================================================================
CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t06_summary_vacancy_region` AS
SELECT
  vs.entity_id_str,
  vs.external_id,
  COALESCE(rl.uk_region, vs.primary_uk_region, 'Unknown') AS uk_region,
  rl.raw_location,
  rl.town_city,
  vs.first_event_date,
  vs.last_event_date,
  vs.clicks,
  vs.applies,
  vs.title,
  vs.organization_name,
  vs.occupational_fields,
  vs.importer_ID,
  vs.importer_name,
  vs.workflow_state,
  vs.upgrades,
  vs.start_date,
  vs.end_date,
  vs.category,
  vs.contract_type,
  vs.employment_type,
  vs.min_salary,
  vs.max_salary,
  vs.currency_code,
  vs.salary_free_text,
  vs.salary_exact,
  vs.salary_unit,
  vs.sites
FROM `site-monitoring-421401.JPD.t06_summary_vacancy` vs
LEFT JOIN _vacancy_locations_resolved rl
  ON vs.entity_id_str = rl.entity_id
  AND rl.uk_region IS NOT NULL;


-- ===========================================================================
-- T3: t06_summary_daily_totals — one row per day.
-- GA4 clicks/applies (SUM Events) + JGP/LG split, GSC site + rich-result
-- metrics (from the pre-aggregated t04_gsc_daily), and live
-- vacancy counts from the vacancy summary's start/end dates.
-- ===========================================================================
CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t06_summary_daily_totals`
PARTITION BY event_date
AS
WITH
daily_events AS (
  SELECT
    event_date_dt AS event_date,
    SUM(IF(event_name = 'job_visit',                                Events, 0)) AS clicks,
    SUM(IF(event_name = 'job_visit'       AND site = 'Jobs Go Public', Events, 0)) AS clicks_jgp,
    SUM(IF(event_name = 'job_visit'       AND site = 'LG Jobs',        Events, 0)) AS clicks_lg,
    SUM(IF(event_name = 'job_apply_start',                          Events, 0)) AS applies,
    SUM(IF(event_name = 'job_apply_start' AND site = 'Jobs Go Public', Events, 0)) AS applies_jgp,
    SUM(IF(event_name = 'job_apply_start' AND site = 'LG Jobs',        Events, 0)) AS applies_lg
  FROM `site-monitoring-421401.JPD.t05_enriched_vacancies`
  WHERE event_name IN ('job_visit', 'job_apply_start') AND event_date_dt IS NOT NULL
  GROUP BY event_date_dt
),
-- GSC site-level metrics per day — read from the pre-aggregated, incrementally
-- maintained t04_gsc_daily (04_create_and_backfill_gsc.sql + 04_sync_gsc_daily.sql)
-- instead of re-scanning ~5 GB of raw GSC on every rebuild.
gsc_site_daily AS (
  SELECT event_date,
    impressions_jgp, impressions_lg, gb_impressions_jgp, gb_impressions_lg,
    gsc_clicks_jgp, gsc_clicks_lg, gb_gsc_clicks_jgp, gb_gsc_clicks_lg,
    sum_position_jgp, sum_position_lg
  FROM `site-monitoring-421401.JPD.t04_gsc_daily`
),
-- GSC URL-level rich-result counts — also from the pre-aggregated t04_gsc_daily.
gsc_rich_daily AS (
  SELECT event_date,
    job_listing_rich_jgp, job_listing_rich_lg, job_detail_rich_jgp, job_detail_rich_lg
  FROM `site-monitoring-421401.JPD.t04_gsc_daily`
),
date_spine AS (
  SELECT d AS event_date
  FROM UNNEST(GENERATE_DATE_ARRAY(
    (SELECT MIN(event_date) FROM daily_events),
    CURRENT_DATE()
  )) AS d
),
daily_active AS (
  -- Inequality (range) join: O(days × vacancies). Builds in seconds at current
  -- scale and the spine grows only ~1 day/day, so it's fine for now. If it ever
  -- approaches the CI timeout, replace with a sweep-line (delta +1 at start_date,
  -- -1 at end_date+1, cumulative sum over the spine) — kept as a range join here
  -- because it's exact and obvious, and a rewrite risks the active_vacancies number.
  SELECT
    ds.event_date,
    COUNT(DISTINCT vs.entity_id_str) AS active_vacancies,
    COUNT(DISTINCT IF(vs.sites LIKE '%Jobs Go Public%', vs.entity_id_str, NULL)) AS active_jgp,
    COUNT(DISTINCT IF(vs.sites LIKE '%LG Jobs%',        vs.entity_id_str, NULL)) AS active_lg
  FROM date_spine ds
  LEFT JOIN `site-monitoring-421401.JPD.t06_summary_vacancy` vs
    ON ds.event_date >= DATE(vs.start_date)
    AND (ds.event_date <= DATE(vs.end_date) OR vs.end_date IS NULL)
  GROUP BY ds.event_date
)
SELECT
  da.event_date,
  COALESCE(gs.impressions_jgp, 0) + COALESCE(gs.impressions_lg, 0) AS impressions,
  COALESCE(gs.impressions_jgp, 0) AS impressions_jgp,
  COALESCE(gs.impressions_lg, 0)  AS impressions_lg,
  COALESCE(gs.gb_impressions_jgp, 0) AS gb_impressions_jgp,
  COALESCE(gs.gb_impressions_lg, 0)  AS gb_impressions_lg,
  COALESCE(gs.gsc_clicks_jgp, 0) + COALESCE(gs.gsc_clicks_lg, 0) AS gsc_clicks,
  COALESCE(gs.gsc_clicks_jgp, 0) AS gsc_clicks_jgp,
  COALESCE(gs.gsc_clicks_lg, 0)  AS gsc_clicks_lg,
  COALESCE(gs.gb_gsc_clicks_jgp, 0) AS gb_gsc_clicks_jgp,
  COALESCE(gs.gb_gsc_clicks_lg, 0)  AS gb_gsc_clicks_lg,
  ROUND(SAFE_DIVIDE(gs.sum_position_jgp, gs.impressions_jgp), 1) AS avg_position_jgp,
  ROUND(SAFE_DIVIDE(gs.sum_position_lg, gs.impressions_lg), 1)   AS avg_position_lg,
  COALESCE(gs.sum_position_jgp, 0) AS sum_position_jgp,
  COALESCE(gs.sum_position_lg, 0)  AS sum_position_lg,
  COALESCE(gr.job_listing_rich_jgp, 0) AS job_listing_rich_jgp,
  COALESCE(gr.job_listing_rich_lg, 0)  AS job_listing_rich_lg,
  COALESCE(gr.job_detail_rich_jgp, 0)  AS job_detail_rich_jgp,
  COALESCE(gr.job_detail_rich_lg, 0)   AS job_detail_rich_lg,
  COALESCE(de.clicks, 0)     AS clicks,
  COALESCE(de.clicks_jgp, 0) AS clicks_jgp,
  COALESCE(de.clicks_lg, 0)  AS clicks_lg,
  COALESCE(de.applies, 0)     AS applies,
  COALESCE(de.applies_jgp, 0) AS applies_jgp,
  COALESCE(de.applies_lg, 0)  AS applies_lg,
  da.active_vacancies,
  da.active_jgp,
  da.active_lg
FROM daily_active da
LEFT JOIN daily_events  de ON da.event_date = de.event_date
LEFT JOIN gsc_site_daily gs ON da.event_date = gs.event_date
LEFT JOIN gsc_rich_daily gr ON da.event_date = gr.event_date;


-- ===========================================================================
-- T4: t06_summary_media — one row per (vacancy x source x medium x campaign).
-- Drives the Client Report media-performance section.
-- ===========================================================================
CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t06_summary_media` AS
WITH agg AS (
  SELECT
    entity_id,
    importer_ID,
    source,
    medium,
    campaign,
    SUM(IF(event_name = 'job_visit',       Events, 0)) AS clicks,
    SUM(IF(event_name = 'job_apply_start', Events, 0)) AS applies
  FROM `site-monitoring-421401.JPD.t05_enriched_vacancies`
  WHERE event_name IN ('job_visit', 'job_apply_start') AND entity_id IS NOT NULL
  GROUP BY entity_id, importer_ID, source, medium, campaign
)
SELECT
  a.entity_id AS entity_id_str,
  a.importer_ID,
  imp.importer_name,
  a.source,
  a.medium,
  a.campaign,
  a.clicks,
  a.applies
FROM agg a
LEFT JOIN `site-monitoring-421401.JPD.t04_importers` imp
  ON a.importer_ID = imp.importer_id;
