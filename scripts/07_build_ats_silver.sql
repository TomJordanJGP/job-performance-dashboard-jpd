-- Silver for the ATS API layer. Parses the raw JSON in JPD.t01_ats_api into two
-- flat, query-friendly tables. Full rebuild each run (CREATE OR REPLACE) — cheap
-- (~100 MB Bronze). Runs after 07_sync_ats_api.py. Consumed by Gold (t06_*).
--
--   t04_ats_media_source  — one row per (vacancy x media_supplier); the 6 "Media
--                           ROI" metrics (direct+network summed) + submitted.
--   t04_ats_application   — one flat row per application; current status + derived
--                           funnel (reached_* via a stage-rank), outcome, timings.
--
-- Supplier names are normalised into media_supplier_key (lower-case, ALL
-- non-alphanumerics stripped) so "Findajob"/"findajob"/"Find a Job" and
-- trailing-space/case variants group as one channel; the raw display name is kept
-- as media_supplier.
-- Timestamps parse to UTC. Status set is treated as OPEN (unknowns bucket to
-- 'in_progress' / rank 0).

-- ============================================================================
-- 1. t04_ats_media_source  (grain: vacancy x media_supplier)
-- ============================================================================
CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t04_ats_media_source`
CLUSTER BY jgp_external_vacancy_id AS
SELECT
  s.jgp_external_vacancy_id,
  s.ats_vacancy_id,
  s.fetched_at,
  TRIM(JSON_VALUE(ms, '$.media_supplier.name')) AS media_supplier,
  -- normalise for grouping/joining: lower-case + strip ALL non-alphanumerics, so
  -- "Find a Job"/"findajob" and "Jobs -Go -Public"/"Jobs Go Public" collapse to one
  -- key, while genuinely distinct products ("LinkedIn Advertising" vs "LinkedIn job
  -- slot", "X" vs "X Internal") stay separate.
  REGEXP_REPLACE(LOWER(IFNULL(JSON_VALUE(ms, '$.media_supplier.name'), '')), r'[^a-z0-9]', '') AS media_supplier_key,

  -- List Views = list_count + network_list_count
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.list_count') AS INT64), 0)
    + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_list_count') AS INT64), 0) AS list_views,

  -- Advert Views = 5 direct visit counts + 5 network visit counts
  ( IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.search_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.list_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.email_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.feed_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.other_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_search_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_list_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_email_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_feed_visit_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_other_visit_count') AS INT64), 0) ) AS advert_views,

  -- Apply Clicks = apply_click_count + network_apply_click_count
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.apply_click_count') AS INT64), 0)
    + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_apply_click_count') AS INT64), 0) AS apply_clicks,

  -- Applications Started = online + external + downloaded (+ network each)
  ( IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.online_application_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.external_application_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.downloaded_application_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_online_application_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_external_application_count') AS INT64), 0)
  + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_downloaded_application_count') AS INT64), 0) ) AS applications_started,

  -- Form Downloads = downloaded_application_count + network
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.downloaded_application_count') AS INT64), 0)
    + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_downloaded_application_count') AS INT64), 0) AS form_downloads,

  -- Attachment Downloads = downloaded_attachment_count + network
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.downloaded_attachment_count') AS INT64), 0)
    + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_downloaded_attachment_count') AS INT64), 0) AS attachment_downloads,

  -- Submitted Applications (the real completed-application count; not in the six)
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.submitted_application_count') AS INT64), 0)
    + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_submitted_application_count') AS INT64), 0) AS submitted_applications,

  -- Engagement extras (captured, not surfaced initially)
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.saved_job_count') AS INT64), 0)
    + IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.network_saved_job_count') AS INT64), 0) AS saved_jobs,
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.sent_to_a_friend_count') AS INT64), 0) AS sent_to_friend,
  IFNULL(SAFE_CAST(JSON_VALUE(ms, '$.jbe_count') AS INT64), 0) AS jbe
FROM `site-monitoring-421401.JPD.t01_ats_api` s,
     UNNEST(JSON_QUERY_ARRAY(s.payload, '$.vacancy_media_sources')) AS ms
WHERE s.payload IS NOT NULL AND s.http_status = 200;

-- ============================================================================
-- 2. t04_ats_application  (grain: one row per application)
-- ============================================================================
CREATE OR REPLACE TABLE `site-monitoring-421401.JPD.t04_ats_application`
CLUSTER BY jgp_external_vacancy_id AS
WITH apps AS (
  SELECT
    s.jgp_external_vacancy_id,
    s.fetched_at,
    SAFE_CAST(JSON_VALUE(a, '$.id') AS INT64) AS application_id,
    SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', JSON_VALUE(a, '$.created_at')) AS created_at,
    JSON_VALUE(a, '$.vacancy_application_status.name') AS current_status,
    JSON_VALUE(a, '$.referrer.media_supplier.name') AS referrer_supplier_raw,
    JSON_QUERY_ARRAY(a, '$.vacancy_application_status_histories') AS hist_arr
  FROM `site-monitoring-421401.JPD.t01_ats_api` s,
       UNNEST(JSON_QUERY_ARRAY(s.payload, '$.vacancy_applications')) AS a
  WHERE s.payload IS NOT NULL AND s.http_status = 200
),
hist_flat AS (
  SELECT
    apps.jgp_external_vacancy_id,
    apps.application_id,
    JSON_VALUE(h, '$.vacancy_application_status.name') AS h_status,
    SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S%Ez', JSON_VALUE(h, '$.created_at')) AS h_ts
  FROM apps, UNNEST(apps.hist_arr) AS h
),
hist_agg AS (
  SELECT
    jgp_external_vacancy_id,
    application_id,
    -- forward stage rank; terminal negatives + unknowns -> 0 (don't advance)
    MAX(CASE h_status
          WHEN 'Applied' THEN 1 WHEN 'Shortlisted' THEN 2 WHEN 'Interview' THEN 3
          WHEN 'Conditional Offer' THEN 4 WHEN 'Reserve' THEN 4 WHEN 'Recruited' THEN 5
          ELSE 0 END) AS max_hist_rank,
    MIN(IF(h_status = 'Applied', h_ts, NULL)) AS applied_at_hist,
    MIN(IF(h_status = 'Recruited', h_ts, NULL)) AS recruited_at
  FROM hist_flat
  GROUP BY jgp_external_vacancy_id, application_id
),
derived AS (
  SELECT
    ap.jgp_external_vacancy_id,
    ap.application_id,
    ap.created_at,
    ap.current_status,
    TRIM(ap.referrer_supplier_raw) AS referrer_supplier,
    REGEXP_REPLACE(LOWER(IFNULL(ap.referrer_supplier_raw, '')), r'[^a-z0-9]', '') AS referrer_supplier_key,
    ap.current_status NOT IN ('Applicant Editing Application', 'Requested Pack') AS is_submitted,
    CASE
      WHEN ap.current_status = 'Recruited' THEN 'hired'
      WHEN ap.current_status = 'Unsuccessful' THEN 'unsuccessful'
      WHEN ap.current_status IN ('Withdrawn', 'Offer Withdrawn') THEN 'withdrawn'
      WHEN ap.current_status IN ('Applicant Editing Application', 'Requested Pack') THEN 'draft'
      ELSE 'in_progress'
    END AS outcome,
    -- effective stage: a submitted app reached >= Applied(1); plus best of history
    -- and current-status rank. Robust to the noisy / out-of-order histories.
    GREATEST(
      IF(ap.current_status NOT IN ('Applicant Editing Application', 'Requested Pack'), 1, 0),
      IFNULL(hg.max_hist_rank, 0),
      CASE ap.current_status
        WHEN 'Applied' THEN 1 WHEN 'Shortlisted' THEN 2 WHEN 'Interview' THEN 3
        WHEN 'Conditional Offer' THEN 4 WHEN 'Reserve' THEN 4 WHEN 'Recruited' THEN 5
        ELSE 0 END
    ) AS max_stage_rank,
    COALESCE(hg.applied_at_hist, ap.created_at) AS applied_at,
    hg.recruited_at,
    ap.fetched_at
  FROM apps ap
  LEFT JOIN hist_agg hg
    ON hg.jgp_external_vacancy_id = ap.jgp_external_vacancy_id
   AND hg.application_id = ap.application_id
)
SELECT
  jgp_external_vacancy_id,
  application_id,
  created_at,
  current_status,
  referrer_supplier,
  referrer_supplier_key,
  is_submitted,
  outcome,
  max_stage_rank,
  max_stage_rank >= 1 AS reached_applied,
  max_stage_rank >= 2 AS reached_shortlisted,
  max_stage_rank >= 3 AS reached_interview,
  max_stage_rank >= 4 AS reached_offer,
  max_stage_rank >= 5 AS reached_recruited,
  applied_at,
  recruited_at,
  IF(recruited_at IS NOT NULL, TIMESTAMP_DIFF(recruited_at, applied_at, DAY), NULL) AS time_to_hire_days,
  fetched_at
FROM derived;
