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
    COALESCE(LOGICAL_OR(is_live), FALSE) AS is_live,
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
),
-- Feed drop-out date: MAX(last_seen) per entity_id across every feed that carries
-- it (all t01_feed_* except selfservice, which has no feed poll). Keyed on
-- entity_id ONLY. external_id is a content hash, NOT unique: the same hash maps to
-- multiple entity_ids for re-listed/duplicate content, so an external_id match
-- would pull an unrelated live vacancy's last_seen onto a closed one (verified:
-- entity 24103 got today's date from twin entity 67141 that shares its hash). The
-- Appcast overlay backfills entity_id onto the source-feed rows, so entity_id
-- alone gives full coverage — identical to the external_id-inclusive join.
feed_ls_by_entity AS (
  SELECT CAST(entity_id AS STRING) AS entity_id_str, MAX(last_seen) AS ls
  FROM (
              SELECT entity_id, last_seen FROM `site-monitoring-421401.JPD.t01_feed_appcast`
    UNION ALL SELECT entity_id, last_seen FROM `site-monitoring-421401.JPD.t01_feed_ats`
    UNION ALL SELECT entity_id, last_seen FROM `site-monitoring-421401.JPD.t01_feed_scrape`
    UNION ALL SELECT entity_id, last_seen FROM `site-monitoring-421401.JPD.t01_feed_civil_service`
    UNION ALL SELECT entity_id, last_seen FROM `site-monitoring-421401.JPD.t01_feed_backfill`
    UNION ALL SELECT entity_id, last_seen FROM `site-monitoring-421401.JPD.t01_feed_jgp_london_backfill`
  )
  WHERE entity_id IS NOT NULL
  GROUP BY entity_id_str
),
vac AS (
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
  a.is_live,
  -- vacancy_status: real Published/Unpublished status. workflow_state is copied
  -- verbatim from the Appcast feed (≈always 'published') and is never flipped when
  -- a vacancy drops out, so it can't be trusted. is_live (in the feed within 24h)
  -- is the true signal; the feed only carries published rows, so this is exactly
  -- the Published/Unpublished pair the source system uses.
  CASE WHEN a.is_live THEN 'Published' ELSE 'Unpublished' END AS vacancy_status,
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
  a.sites,
  -- feed drop-out date: this entity's last appearance in any feed
  DATE(be.ls) AS feed_last_seen,
  -- Resolved close-date estimate for a dropped vacancy = the LATER of the feed
  -- drop-out date (last_seen) and the last GA4 interaction (last_event_date):
  -- "the last date we have ANY evidence the vacancy was alive". GA4
  -- job_visit/job_apply_start are human page interactions (bots don't fire GA4
  -- JS events), so a click after the feed dropped the job is real evidence it was
  -- still live. Just as important: backfilled/already-expired jobs are seen in a
  -- SINGLE feed poll, so first_seen == last_seen == ingest ≈ start_date — the feed
  -- date alone would make close == start. GREATEST fixes both cases.
  CASE
    WHEN DATE(be.ls) IS NULL        THEN a.last_event_date
    WHEN a.last_event_date IS NULL  THEN DATE(be.ls)
    ELSE GREATEST(DATE(be.ls), a.last_event_date)
  END AS est_close,
  CASE
    WHEN DATE(be.ls) IS NULL              THEN 'last_event'
    WHEN a.last_event_date IS NULL        THEN 'feed_dropout'
    WHEN a.last_event_date >= DATE(be.ls) THEN 'last_event'
    ELSE 'feed_dropout'
  END AS est_src
FROM agg a
LEFT JOIN regions r USING (entity_id)
LEFT JOIN `site-monitoring-421401.JPD.t04_importers` imp
  ON a.importer_ID = imp.importer_id
LEFT JOIN feed_ls_by_entity be ON be.entity_id_str = a.entity_id
)
SELECT
  vac.* EXCEPT (end_date, est_close, est_src),
  -- end_date is now the BEST-AVAILABLE close date, not the raw feed value: real
  -- end_date wins; a still-live vacancy with no end_date stays open (NULL);
  -- otherwise the vacancy dropped out, so fill from est_close (the LATER of feed
  -- drop-out and last GA4 interaction — see the vac CTE). Kept as TIMESTAMP (the
  -- original column type) so EVERY dashboard version renders it with no code
  -- change — the value lives in the field the app already reads. The untouched
  -- feed value is preserved as end_date_actual for provenance / strict analysis.
  vac.end_date AS end_date_actual,
  -- Estimate is floored at start_date so a close date can NEVER precede the start
  -- (estimates land at midnight, start_date carries a time, so a same-day estimate
  -- would otherwise read a few hours before start). Floors to exactly start_date in
  -- that case (0 days), never negative.
  CASE
    WHEN vac.end_date IS NOT NULL   THEN vac.end_date
    WHEN vac.is_live               THEN NULL
    WHEN vac.start_date IS NOT NULL THEN GREATEST(TIMESTAMP(vac.est_close), vac.start_date)
    ELSE TIMESTAMP(vac.est_close)
  END AS end_date,
  -- end_date_est: DATE mirror of the resolved close date (kept for the newer view
  -- code / any consumer that prefers a DATE); identical rule to end_date above.
  CASE
    WHEN vac.end_date IS NOT NULL   THEN DATE(vac.end_date)
    WHEN vac.is_live               THEN NULL
    WHEN vac.start_date IS NOT NULL THEN GREATEST(vac.est_close, DATE(vac.start_date))
    ELSE vac.est_close
  END AS end_date_est,
  -- provenance for the resolved close date, so the UI can distinguish estimates
  CASE
    WHEN vac.end_date IS NOT NULL THEN 'actual'
    WHEN vac.is_live             THEN 'still_live'
    ELSE vac.est_src
  END AS end_date_source
FROM vac;


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
vac_frontends AS (
  -- Board-membership signal from the source feeds: `frontends` is a comma-
  -- separated list of the JGP sites a vacancy is posted on (jobsgopublic /
  -- lgjobs). Keyed by external_id; covers ~98% of LIVE vacancies. Older expired
  -- vacancies predate frontends population (mostly NULL) — those fall back to the
  -- GA4 `sites` (traffic) signal below. Appcast (the master) does not emit
  -- frontends, so this reads the source-feed Bronze tables directly.
  SELECT external_id, ANY_VALUE(frontends) AS frontends FROM (
    SELECT external_id, frontends FROM `site-monitoring-421401.JPD.t01_feed_ats`                 WHERE frontends IS NOT NULL
    UNION ALL SELECT external_id, frontends FROM `site-monitoring-421401.JPD.t01_feed_scrape`              WHERE frontends IS NOT NULL
    UNION ALL SELECT external_id, frontends FROM `site-monitoring-421401.JPD.t01_feed_civil_service`       WHERE frontends IS NOT NULL
    UNION ALL SELECT external_id, frontends FROM `site-monitoring-421401.JPD.t01_feed_backfill`            WHERE frontends IS NOT NULL
    UNION ALL SELECT external_id, frontends FROM `site-monitoring-421401.JPD.t01_feed_jgp_london_backfill` WHERE frontends IS NOT NULL
  )
  GROUP BY external_id
),
vac_effective AS (
  -- Per-vacancy effective close date + board membership.
  --
  -- Close date — the ~39% of vacancies with no explicit end_date otherwise never
  -- expire from the live count, inflating it ~5x (24k shown vs ~4.5k truly live).
  -- Uses the SAME estimator as the vacancy table: end_date_est (real end_date ->
  -- if live: open -> feed drop-out date -> last GA4 interaction). Feed drop-out
  -- (last_seen) is preferred over last_event_date because last_event trails the
  -- true close by ~20 days median (residual/crawler traffic on an expired page):
  --   * has explicit end_date         -> trust it            (end_date_est = actual)
  --   * no end_date, still in feed     -> open (NULL)          (still_live)
  --   * no end_date, dropped from feed -> feed drop-out date   (feed_dropout)
  --                                       last GA4 interaction where feed date
  --                                       is missing (~0.3%)   (last_event)
  --   * no end_date, no signal at all  -> closed at start_date (edge; ~0 rows today)
  --
  -- Board (on_jgp / on_lg) — the feed `frontends` list is authoritative; GA4
  -- `sites` (traffic) is the fallback so a live vacancy with no clicks yet still
  -- lands on the right board, and older expired vacancies without frontends still
  -- split. A vacancy can be on both boards (counts in both bars; active_vacancies
  -- is the deduplicated total).
  SELECT
    v.entity_id_str,
    DATE(v.start_date) AS start_d,
    CASE
      WHEN v.is_live THEN v.end_date_est                       -- open (NULL) unless a real end_date
      ELSE COALESCE(v.end_date_est, DATE(v.start_date))        -- dropped: estimate, floored at start
    END AS end_d,
    COALESCE(f.frontends LIKE '%jobsgopublic%' OR v.sites LIKE '%Jobs Go Public%', FALSE) AS on_jgp,
    COALESCE(f.frontends LIKE '%lgjobs%'       OR v.sites LIKE '%LG Jobs%',        FALSE) AS on_lg
  FROM `site-monitoring-421401.JPD.t06_summary_vacancy` v
  LEFT JOIN vac_frontends f ON v.external_id = f.external_id
),
daily_active AS (
  -- "Live on day D" = D falls between the vacancy's start and its effective close
  -- date (vac_effective). active_jgp/active_lg split by board membership (a both-
  -- boards vacancy counts in both; active_vacancies is the dedup total). Inequality
  -- (range) join: O(days × vacancies); builds in seconds at current scale and the
  -- spine grows ~1 day/day. If it ever nears the CI timeout, replace with a
  -- sweep-line (delta +1 at start, -1 at end+1, cumulative sum over the spine) —
  -- kept as a range join here because it's exact and obvious.
  SELECT
    ds.event_date,
    COUNT(DISTINCT ve.entity_id_str) AS active_vacancies,
    COUNT(DISTINCT IF(ve.on_jgp, ve.entity_id_str, NULL)) AS active_jgp,
    COUNT(DISTINCT IF(ve.on_lg,  ve.entity_id_str, NULL)) AS active_lg
  FROM date_spine ds
  LEFT JOIN vac_effective ve
    ON ds.event_date >= ve.start_d
    AND (ds.event_date <= ve.end_d OR ve.end_d IS NULL)
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
