#!/usr/bin/env python3
"""Bronze ingest — upsert the 5 job feeds into JPD.t01_feed_* current-state tables.

Each table holds ONE row per vacancy, kept forever (a history of every vacancy that
has ever appeared), always showing the latest field values. This is NOT a per-day
snapshot and NOT a per-field change log — it's an upsert via MERGE:
  - new vacancy            -> INSERT (first_seen = run time)
  - vacancy seen again     -> UPDATE fields to latest + last_seen = run time
  - field value changed    -> last_updated bumped (detected via a content hash)
  - vacancy left the feed   -> row KEPT (last_seen stops advancing -> "no longer live")
"Currently live in feed X" = last_seen == MAX(last_seen) for that feed. The true
go-live date is the feed's own start_date/date_posted, independent of poll timing —
so polling can run several times a day cheaply (only genuinely-new rows are added).

Feeds (root <jobs> -> many <job>):
  - Appcast (Jobiqo)  — registry. <id> = entity_id (always present), <external_id> = hash.
  - Scrape / ATS / Civil Service / Backfill — <id> = hash (external_id), no entity_id.
The four source feeds are mutually exclusive; Appcast overlays ~99% and supplies entity_id.

MERGE match key:
  - source feeds: external_id (always present, stable).
  - Appcast: entity_id OR external_id — a site-created job has only entity_id (no hash
    yet), so match on whichever is present; external_id is then filled when it appears.
  Each poll is de-duped to one row per key before the MERGE (avoids the duplicate-row
  trap recorded in lessons.md).

Location: nested ARRAY<STRUCT<country, region, postcode, city, street, formatted_address>>
per feed (Appcast/CS multi-site up to 50; source feeds single-site). Region DERIVATION
is deferred — Bronze captures raw location as-is. UNNEST gives one row per location later.

Usage:
    venv/bin/python scripts/01_sync_bronze_feeds.py --dry-run   # fetch+parse+report, no BQ
    venv/bin/python scripts/01_sync_bronze_feeds.py             # real run (upserts to JPD)
"""

import os
import sys
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from dateutil import parser as dateparser
import requests

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"
BQ_DATASET = "JPD"

FEEDS = {
    "Appcast": "https://redacted.invalid/appcast_feed.xml",
    "Scrape": "https://storage.googleapis.com/scrpr-job-data-export/jgp_scrapes/jgp_scraping_feed.xml",
    "Civil Service": "https://storage.googleapis.com/scrpr-job-data-export/jgp_scrapes/civil_service_jobs_uk.xml",
    "ATS": "https://storage.googleapis.com/scrpr-job-data-export/jgp_scrapes/jgp_ats_feed.xml",
    "Backfill": "https://storage.googleapis.com/scrpr-job-data-export/jgp_scrapes/jgp_backfill_feed_v1.xml",
}

TABLE_NAMES = {
    "Appcast": "t01_feed_appcast", "Scrape": "t01_feed_scrape",
    "Civil Service": "t01_feed_civil_service", "ATS": "t01_feed_ats", "Backfill": "t01_feed_backfill",
}

# MERGE config per feed: match condition, the key the poll is de-duped on, and the
# stable key column that must NOT be overwritten by an UPDATE.
MERGE_CFG = {
    "Appcast": dict(match="T.entity_id = S.entity_id OR T.external_id = S.external_id",
                    dedup_key="entity_id", stable="entity_id", cluster=["entity_id", "external_id"]),
    "Scrape": dict(match="T.external_id = S.external_id",
                   dedup_key="external_id", stable="external_id", cluster=["external_id"]),
    "Civil Service": dict(match="T.external_id = S.external_id",
                          dedup_key="external_id", stable="external_id", cluster=["external_id"]),
    "ATS": dict(match="T.external_id = S.external_id",
                dedup_key="external_id", stable="external_id", cluster=["external_id"]),
    "Backfill": dict(match="T.external_id = S.external_id",
                     dedup_key="external_id", stable="external_id", cluster=["external_id"]),
}


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def get_client():
    from google.oauth2.service_account import Credentials
    from google.cloud import bigquery

    sa_path = os.path.join(project_dir, 'service_account.json')
    if not os.path.exists(sa_path):
        print(f"ERROR: service_account.json not found at {sa_path}")
        sys.exit(1)
    creds = Credentials.from_service_account_file(
        sa_path, scopes=['https://www.googleapis.com/auth/bigquery'])
    return bigquery.Client(credentials=creds, project=BQ_PROJECT)


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #
def get_text(element, tag, default=None):
    child = element.find(tag)
    if child is not None and child.text and child.text.strip():
        return child.text.strip()
    return default


def get_all_text(element, tag):
    vals = [c.text.strip() for c in element.findall(tag) if c.text and c.text.strip()]
    return ' | '.join(vals) if vals else None


def get_nested_text(element, parent_tag, child_tag, default=None):
    parent = element.find(parent_tag)
    if parent is not None:
        child = parent.find(child_tag)
        if child is not None and child.text and child.text.strip():
            return child.text.strip()
    return default


def parse_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def parse_dt(date_str):
    """Feed date string -> UTC ISO-8601 (BQ TIMESTAMP-ready) or None."""
    if not date_str or not date_str.strip():
        return None
    try:
        dt = dateparser.parse(date_str.strip())
    except Exception:
        return None
    if dt is None:
        return None
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.isoformat()


def salary_fields(job_el):
    return {
        'salary_min': parse_float(get_nested_text(job_el, 'salary', 'salary_min')),
        'salary_max': parse_float(get_nested_text(job_el, 'salary', 'salary_max')),
        'salary_exact': parse_float(get_nested_text(job_el, 'salary', 'salary_exact')),
        'salary_currency': get_nested_text(job_el, 'salary', 'salary_currency'),
        'salary_type': get_nested_text(job_el, 'salary', 'salary_type'),
    }


LOC_KEYS = ('country', 'region', 'postcode', 'city', 'street', 'formatted_address')


def _location_struct(loc_el):
    return {
        'country': get_text(loc_el, 'country'), 'region': get_text(loc_el, 'region'),
        'postcode': get_text(loc_el, 'postal_code'), 'city': get_text(loc_el, 'city'),
        'street': get_text(loc_el, 'street'),
        'formatted_address': get_text(loc_el, 'formatted_address'),
    }


def parse_locations(job_el):
    container = job_el.find('locations')
    return [_location_struct(loc) for loc in container.findall('location')] if container is not None else []


def parse_cs_locations(job_el):
    ml = job_el.find('multi_locations')
    if ml is None:
        return []
    out = []
    for item in ml.findall('item'):
        txt = (item.text or '').strip()
        if txt:
            s = {k: None for k in LOC_KEYS}
            s['formatted_address'] = txt
            out.append(s)
    return out


# --------------------------------------------------------------------------- #
# Per-feed parsers -> content dicts (no metadata; first_seen/last_seen set in MERGE)
# --------------------------------------------------------------------------- #
def parse_appcast(jobs):
    return [{
        'entity_id': get_text(j, 'id'), 'external_id': get_text(j, 'external_id'),
        'title': get_text(j, 'title'), 'company': get_text(j, 'company'),
        'organization_id': get_text(j, 'organization_id'), 'occupation': get_text(j, 'occupation'),
        'employment_type': get_all_text(j, 'employment_type'),
        'date_posted': parse_dt(get_text(j, 'date')), 'date_end': parse_dt(get_text(j, 'date_end')),
        'remote_option': get_text(j, 'remote_option'), 'workflow_state': get_text(j, 'workflow_state'),
        'application_workflow': get_text(j, 'application_workflow'),
        'application_information': get_text(j, 'application_information'),
        'logo_url': get_text(j, 'logo_url'), 'url': get_text(j, 'url'),
        'locations': parse_locations(j),
    } for j in jobs]


def parse_scrape(jobs):
    """Also used for Backfill (identical shape)."""
    out = []
    for j in jobs:
        rec = {
            'external_id': get_text(j, 'id'), 'entity_id': None,
            'title': get_text(j, 'title'), 'url': get_text(j, 'url'),
            'apply_url': get_text(j, 'apply_url'), 'job_location': get_text(j, 'job_location'),
            'organization_id': get_text(j, 'organization_id'),
            'organization_name': get_text(j, 'organization_name'),
            'organization_type': get_text(j, 'organization_type'),
            'occupation': get_text(j, 'occupation'), 'category': get_text(j, 'category'),
            'contract_type': get_text(j, 'contract_type'), 'workplace': get_text(j, 'workplace'),
            'working_pattern': get_text(j, 'working_pattern'), 'working_hours': get_text(j, 'working_hours'),
            'salary_free_text': get_text(j, 'salary_free_text'),
            'salary_range_from': get_text(j, 'salary_range_from'),
            'salary_range_to': get_text(j, 'salary_range_to'),
            'reference': get_text(j, 'reference'), 'old_account_id': get_text(j, 'old_account_id'),
            'frontends': get_text(j, 'frontends'),
            'start_date': parse_dt(get_text(j, 'start_date')),
            'close_date': parse_dt(get_text(j, 'close_date')),
            'locations': parse_locations(j),
        }
        rec.update(salary_fields(j))
        out.append(rec)
    return out


def parse_civil_service(jobs):
    out = []
    for j in jobs:
        rec = {
            'external_id': get_text(j, 'id'), 'entity_id': None,
            'jobiqo_org_id': get_text(j, 'jobiqo_id'),
            'title': get_text(j, 'title'), 'url': get_text(j, 'url'),
            'apply_url': get_text(j, 'apply_url'), 'location': get_text(j, 'location'),
            'occupation': get_text(j, 'occupation'), 'category': get_text(j, 'category'),
            'contract_type': get_text(j, 'contract_type'), 'workplace': get_text(j, 'workplace'),
            'working_pattern': get_text(j, 'working_pattern'),
            'salary_free_text': get_text(j, 'salary_free_text'),
            'reference': get_text(j, 'reference'), 'frontends': get_text(j, 'frontends'),
            'start_date': parse_dt(get_text(j, 'start_date')),
            'close_date': parse_dt(get_text(j, 'close_date')),
            'locations': parse_cs_locations(j),
        }
        rec.update(salary_fields(j))
        out.append(rec)
    return out


def parse_ats(jobs):
    out = []
    for j in jobs:
        rec = {
            'external_id': get_text(j, 'id'), 'entity_id': None,
            'title': get_text(j, 'title'), 'url': get_text(j, 'url'),
            'apply_url': get_text(j, 'apply_url'), 'job_location': get_text(j, 'job_location'),
            'organization_id': get_text(j, 'organization_id'),
            'organization_name': get_text(j, 'organization_name'),
            'organization_type': get_text(j, 'organization_type'),
            'occupation': get_text(j, 'occupation'), 'category': get_text(j, 'category'),
            'contract_type': get_text(j, 'contract_type'), 'workplace': get_text(j, 'workplace'),
            'working_pattern': get_text(j, 'working_pattern'), 'working_hours': get_text(j, 'working_hours'),
            'working_hours_per': get_text(j, 'working_hours_per'),
            'working_hours_free_text': get_text(j, 'working_hours_free_text'),
            'salary_free_text': get_text(j, 'salary_free_text'),
            'salary_range_from': get_text(j, 'salary_range_from'),
            'salary_range_to': get_text(j, 'salary_range_to'),
            'reference': get_text(j, 'reference'), 'old_vacancy_id': get_text(j, 'old_vacancy_id'),
            'old_account_id': get_text(j, 'old_account_id'), 'frontends': get_text(j, 'frontends'),
            'internal_external': get_text(j, 'internal_external'),
            'application_method': get_text(j, 'application_method'), 'crb_check': get_text(j, 'crb_check'),
            'created': parse_dt(get_text(j, 'created')), 'updated': parse_dt(get_text(j, 'updated')),
            'start_date': parse_dt(get_text(j, 'start_date')),
            'close_date': parse_dt(get_text(j, 'close_date')),
            'locations': parse_locations(j),
        }
        rec.update(salary_fields(j))
        out.append(rec)
    return out


PARSERS = {"Appcast": parse_appcast, "Scrape": parse_scrape, "Civil Service": parse_civil_service,
           "ATS": parse_ats, "Backfill": parse_scrape}


# --------------------------------------------------------------------------- #
# Schemas — CONTENT (staging) + TARGET (content + hash + first/last/updated)
# --------------------------------------------------------------------------- #
def _content_schema():
    from google.cloud import bigquery
    S = lambda n, t='STRING': bigquery.SchemaField(n, t)
    TS, F = 'TIMESTAMP', 'FLOAT64'
    loc = bigquery.SchemaField('locations', 'RECORD', mode='REPEATED', fields=[
        S('country'), S('region'), S('postcode'), S('city'), S('street'), S('formatted_address')])
    salary = [S('salary_min', F), S('salary_max', F), S('salary_exact', F),
              S('salary_currency'), S('salary_type')]
    scrape_like = [
        S('external_id'), S('entity_id'), S('title'), S('url'), S('apply_url'), S('job_location'),
        S('organization_id'), S('organization_name'), S('organization_type'), S('occupation'),
        S('category'), S('contract_type'), S('workplace'), S('working_pattern'), S('working_hours'),
        S('salary_free_text'), S('salary_range_from'), S('salary_range_to'),
    ] + salary + [S('reference'), S('old_account_id'), S('frontends'),
                  S('start_date', TS), S('close_date', TS), loc]
    return {
        "Appcast": [
            S('entity_id'), S('external_id'), S('title'), S('company'), S('organization_id'),
            S('occupation'), S('employment_type'), S('date_posted', TS), S('date_end', TS),
            S('remote_option'), S('workflow_state'), S('application_workflow'),
            S('application_information'), S('logo_url'), S('url'), loc],
        "Scrape": scrape_like, "Backfill": scrape_like,
        "Civil Service": [
            S('external_id'), S('entity_id'), S('jobiqo_org_id'), S('title'), S('url'), S('apply_url'),
            S('location'), S('occupation'), S('category'), S('contract_type'), S('workplace'),
            S('working_pattern'), S('salary_free_text'),
        ] + salary + [S('reference'), S('frontends'), S('start_date', TS), S('close_date', TS), loc],
        "ATS": [
            S('external_id'), S('entity_id'), S('title'), S('url'), S('apply_url'), S('job_location'),
            S('organization_id'), S('organization_name'), S('organization_type'), S('occupation'),
            S('category'), S('contract_type'), S('workplace'), S('working_pattern'), S('working_hours'),
            S('working_hours_per'), S('working_hours_free_text'), S('salary_free_text'),
            S('salary_range_from'), S('salary_range_to'),
        ] + salary + [S('reference'), S('old_vacancy_id'), S('old_account_id'), S('frontends'),
                      S('internal_external'), S('application_method'), S('crb_check'),
                      S('created', TS), S('updated', TS), S('start_date', TS), S('close_date', TS), loc],
    }


def _target_schema(content):
    from google.cloud import bigquery
    return list(content) + [
        bigquery.SchemaField('content_hash', 'INT64'),
        bigquery.SchemaField('first_seen', 'TIMESTAMP'),
        bigquery.SchemaField('last_seen', 'TIMESTAMP'),
        bigquery.SchemaField('last_updated', 'TIMESTAMP'),
    ]


# --------------------------------------------------------------------------- #
# Fetch + upsert
# --------------------------------------------------------------------------- #
def fetch_feed(feed_name, url):
    print(f"  Fetching {feed_name}...", end=' ', flush=True)
    try:
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        jobs = ET.fromstring(resp.content).findall('.//job')
        print(f"{len(jobs):,} jobs ({len(resp.content) / 1e6:.1f} MB)")
        return jobs
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        return []


def ensure_target(client, feed_name, content_schema):
    from google.cloud import bigquery
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{TABLE_NAMES[feed_name]}"
    try:
        client.get_table(table_id)
    except Exception:
        table = bigquery.Table(table_id, schema=_target_schema(content_schema))
        table.clustering_fields = MERGE_CFG[feed_name]['cluster']
        client.create_table(table)
        print(f"    created {TABLE_NAMES[feed_name]}")
    return table_id


def load_staging(client, feed_name, rows, content_schema):
    from google.cloud import bigquery
    staging_id = f"{BQ_PROJECT}.{BQ_DATASET}.{TABLE_NAMES[feed_name]}_staging"
    job_config = bigquery.LoadJobConfig(write_disposition='WRITE_TRUNCATE', schema=content_schema)
    client.load_table_from_json(rows, staging_id, job_config=job_config).result()
    return staging_id


def run_merge(client, feed_name, target_id, staging_id, content_cols, run_ts):
    cfg = MERGE_CFG[feed_name]
    ts = f'TIMESTAMP("{run_ts}")'
    hash_struct = ', '.join(content_cols)
    set_cols = [c for c in content_cols if c != cfg['stable']]
    set_clause = ',\n      '.join(f'T.{c} = S.{c}' for c in set_cols)
    insert_cols = content_cols + ['content_hash', 'first_seen', 'last_seen', 'last_updated']
    insert_vals = [f'S.{c}' for c in content_cols] + ['S.content_hash', ts, ts, ts]
    sql = f"""
    MERGE `{target_id}` T
    USING (
      SELECT *, FARM_FINGERPRINT(TO_JSON_STRING(STRUCT({hash_struct}))) AS content_hash
      FROM `{staging_id}`
      QUALIFY ROW_NUMBER() OVER (PARTITION BY {cfg['dedup_key']} ORDER BY {cfg['dedup_key']}) = 1
    ) S
    ON {cfg['match']}
    WHEN MATCHED THEN UPDATE SET
      {set_clause},
      last_seen = {ts},
      last_updated = IF(T.content_hash != S.content_hash, {ts}, T.last_updated),
      content_hash = S.content_hash
    WHEN NOT MATCHED THEN INSERT ({', '.join(insert_cols)})
    VALUES ({', '.join(insert_vals)})
    """
    job = client.query(sql)
    job.result()
    client.query(f"DROP TABLE `{staging_id}`").result()
    return job.num_dml_affected_rows


def _pct(num, den):
    return f"{(num / den * 100):.1f}%" if den else "n/a"


def main():
    ap = argparse.ArgumentParser(description='Bronze upsert ingest into JPD.t01_feed_*')
    ap.add_argument('--dry-run', action='store_true', help='Fetch + parse + report only; no BigQuery')
    args = ap.parse_args()

    start = datetime.now()
    run_ts = datetime.now(timezone.utc).isoformat()
    print("Bronze Feed Upsert -> JPD")
    print(f"Started: {start:%Y-%m-%d %H:%M:%S}")

    content_schemas = _content_schema()
    client = None if args.dry_run else get_client()

    frames = {}
    print("\nFetching + parsing feeds...")
    for feed_name, url in FEEDS.items():
        frames[feed_name] = PARSERS[feed_name](fetch_feed(feed_name, url))

    print(f"\n{'='*72}\nRow counts | entity_id linkage | location coverage\n{'='*72}")
    appcast_ids = {r['external_id'] for r in frames.get('Appcast', []) if r.get('external_id')}
    for feed_name, rows in frames.items():
        n = len(rows)
        total_locs = sum(len(r['locations']) for r in rows)
        max_locs = max((len(r['locations']) for r in rows), default=0)
        loc_str = f"locs: {total_locs:,} total, max {max_locs}/vac"
        if feed_name == 'Appcast':
            ent = sum(1 for r in rows if r.get('entity_id'))
            print(f"  {feed_name:14s} {n:6,} rows | entity_id: {ent:,} ({_pct(ent, n)}) | {loc_str}")
        elif n:
            matched = sum(1 for r in rows if r.get('external_id') in appcast_ids)
            print(f"  {feed_name:14s} {n:6,} rows | in Appcast: {matched:,} ({_pct(matched, n)}) | {loc_str}")

    if args.dry_run:
        print(f"\n[DRY RUN] No writes. Parsed {sum(len(d) for d in frames.values()):,} rows total.")
        print(f"Completed in {(datetime.now() - start).total_seconds():.0f}s")
        return

    print(f"\n{'='*72}\nUpserting (MERGE) into Bronze tables\n{'='*72}")
    for feed_name, rows in frames.items():
        if not rows:
            print(f"  {feed_name}: 0 rows — skipped")
            continue
        try:
            cs = content_schemas[feed_name]
            content_cols = [f.name for f in cs]
            target_id = ensure_target(client, feed_name, cs)
            staging_id = load_staging(client, feed_name, rows, cs)
            affected = run_merge(client, feed_name, target_id, staging_id, content_cols, run_ts)
            print(f"  {feed_name:14s} MERGE OK ({affected:,} rows inserted/updated)")
        except Exception as e:
            print(f"  {feed_name:14s} FAILED — {type(e).__name__}: {e}")

    print(f"\n{'='*72}\nCompleted in {(datetime.now() - start).total_seconds():.0f}s")


if __name__ == '__main__':
    main()
