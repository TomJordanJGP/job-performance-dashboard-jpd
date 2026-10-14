-- Recover entity_id for GA4 events that arrived without one (JPD.t04_vacancy_events).
--
-- Why: since ~2026-08-10 the site's GA4 tagging drops entity_id from a growing share of
-- vacancy events (3% mid-Aug -> ~13% by Oct 2026, both sites). 04_sync_ga4_events.sql takes
-- the id from the URL on job pages. The rest fire on listing pages (/jobs?...), organisation
-- pages and saved jobs, where the URL has no id. In July every such event carried an id: they
-- are about one specific vacancy and still carry its details. So each is matched to the
-- vacancy whose events WITH a real id share those details. The root cause is the GA4
-- tagging; this is a stopgap until it is fixed at source.
--
-- Rule: an id-less event gets a vacancy's id only when EXACTLY ONE vacancy has real-id events
-- (entity_id_source 'ga4' or 'url') with the same key within ±7 days of the event's date.
--   Tier 1 key: title, organization_id, owner_id, importer_ID, ats_vacancy_number,
--               salary_low/high, regions, occupations, employment_types (title required).
--   Tier 2 key: ats_account_number + ats_vacancy_number (> 0: -1 is a "not ATS" placeholder).
-- Tested 2026-10-14 on the 26,532 id-less events since 2026-08-26: tier 1 matched 18,371 and
-- tier 2 a further 1,075; 7,086 stay NULL (counted in daily totals only). With 10% of
-- vacancies held out of the lookup, their events matched some other vacancy 0.97% (tier 1) /
-- 0.26% (tier 2) of the time -- the worst case, a vacancy never seen with a real id. Where
-- both tiers match they never disagree. A looser title+organisation+owner key was rejected
-- (9.4% wrong matches).
--
-- Each run first clears the previous run's matches, so a match only ever rests on real ids
-- (never on an earlier match) and is re-derived as new evidence arrives. ~0.5 GB per run.

-- Step 1: clear the previous run's inferred ids.
UPDATE `site-monitoring-421401.JPD.t04_vacancy_events`
SET entity_id = NULL, entity_id_source = NULL
WHERE entity_id_source IN ('match_fingerprint', 'match_ats');

-- Step 2: tier 1 -- the vacancy's full set of details.
UPDATE `site-monitoring-421401.JPD.t04_vacancy_events` AS t
SET entity_id = m.entity_id, entity_id_source = 'match_fingerprint'
FROM (
  WITH keyed AS (
    SELECT entity_id, entity_id_source, event_date_dt AS d,
           TO_JSON_STRING(STRUCT(title, organization_id, owner_id, importer_ID, ats_vacancy_number,
                                 salary_low, salary_high, regions, occupations, employment_types)) AS k
    FROM `site-monitoring-421401.JPD.t04_vacancy_events`
    WHERE title IS NOT NULL
  )
  SELECT i.k, i.d, ANY_VALUE(r.entity_id) AS entity_id
  FROM (SELECT DISTINCT k, d FROM keyed WHERE entity_id IS NULL) AS i
  JOIN (SELECT DISTINCT k, d, entity_id FROM keyed WHERE entity_id_source IN ('ga4', 'url')) AS r
    ON r.k = i.k AND r.d BETWEEN DATE_SUB(i.d, INTERVAL 7 DAY) AND DATE_ADD(i.d, INTERVAL 7 DAY)
  GROUP BY i.k, i.d
  HAVING COUNT(DISTINCT r.entity_id) = 1
) AS m
WHERE t.entity_id IS NULL
  AND t.title IS NOT NULL
  AND t.event_date_dt = m.d
  AND TO_JSON_STRING(STRUCT(t.title, t.organization_id, t.owner_id, t.importer_ID, t.ats_vacancy_number,
                            t.salary_low, t.salary_high, t.regions, t.occupations, t.employment_types)) = m.k;

-- Step 3: tier 2 -- ATS account + vacancy number, for events tier 1 left unmatched.
UPDATE `site-monitoring-421401.JPD.t04_vacancy_events` AS t
SET entity_id = m.entity_id, entity_id_source = 'match_ats'
FROM (
  WITH keyed AS (
    SELECT entity_id, entity_id_source, event_date_dt AS d,
           TO_JSON_STRING(STRUCT(ats_account_number, ats_vacancy_number)) AS k
    FROM `site-monitoring-421401.JPD.t04_vacancy_events`
    WHERE ats_vacancy_number > 0
  )
  SELECT i.k, i.d, ANY_VALUE(r.entity_id) AS entity_id
  FROM (SELECT DISTINCT k, d FROM keyed WHERE entity_id IS NULL) AS i
  JOIN (SELECT DISTINCT k, d, entity_id FROM keyed WHERE entity_id_source IN ('ga4', 'url')) AS r
    ON r.k = i.k AND r.d BETWEEN DATE_SUB(i.d, INTERVAL 7 DAY) AND DATE_ADD(i.d, INTERVAL 7 DAY)
  GROUP BY i.k, i.d
  HAVING COUNT(DISTINCT r.entity_id) = 1
) AS m
WHERE t.entity_id IS NULL
  AND t.ats_vacancy_number > 0
  AND t.event_date_dt = m.d
  AND TO_JSON_STRING(STRUCT(t.ats_account_number, t.ats_vacancy_number)) = m.k;
