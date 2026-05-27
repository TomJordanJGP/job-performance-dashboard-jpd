#!/usr/bin/env python3
"""Bronze ingest — fetch the 5 job feeds into JPD.t01_feed_* append-history tables.

Bronze is a raw landing zone: EVERY vacancy from each feed is kept exactly as parsed —
no row filtering, no dedup. Cleaning/dedup/key-validation is Silver's job. Tables are
partitioned by ingestion_date (DAY) and clustered by external_id. Re-running on the same
day replaces that day's partition (idempotent), never duplicates it.

Feeds (root <jobs> -> many <job>):
  - Appcast (Jobiqo)  — master ID registry. <id> = entity_id, <external_id> = hash.
  - Scrape            — <id> = hash (external_id), no entity_id.
  - Civil Service     — <id> = hash, org id in <jobiqo_id>, multi-site in <multi_locations>.
  - ATS               — <id> = hash, no entity_id.
  - Backfill          — <id> = hash, no entity_id. Same shape as Scrape.
The four source feeds are mutually exclusive (a vacancy is in exactly one); Appcast overlays
~99% of them and supplies entity_id.

Location model:
  Every feed carries multi-location data, so location is stored as a REPEATED STRUCT
  (one struct per site) rather than flat columns:
      locations ARRAY<STRUCT<country, region, postcode, city, street, formatted_address>>
  - Appcast/ATS/Scrape/Backfill: one struct per <locations><location> (Appcast up to 50,
    fully structured incl. postcode; the source feeds are single-site, postcode varies).
  - Civil Service: one struct per <multi_locations><item> (free text -> formatted_address;
    up to 50 sites; postcode parsing deferred to the region phase).
  This makes the future "one row per vacancy x location" a simple UNNEST, no re-ingest.
  Region DERIVATION is still deferred — Bronze only captures the raw location as-is.

ID normalisation (the one deviation from raw field names): every table exposes
`external_id` (the hash) for clustering/joining; `entity_id` only on Appcast (NULL else).
Appcast <date>/<date_end> -> date_posted/date_end (avoids the SQL DATE type-name clash).
Heavy unused HTML (description, how_to_apply, logos, documents) is excluded.

Usage:
    venv/bin/python scripts/01_sync_bronze_feeds.py --dry-run   # fetch+parse+report, no BQ
    venv/bin/python scripts/01_sync_bronze_feeds.py             # real run (writes to JPD)
"""

import os
import sys
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, date, timezone

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
    "Appcast": "t01_feed_appcast",
    "Scrape": "t01_feed_scrape",
    "Civil Service": "t01_feed_civil_service",
    "ATS": "t01_feed_ats",
    "Backfill": "t01_feed_backfill",
}


# --------------------------------------------------------------------------- #
# BigQuery client
# --------------------------------------------------------------------------- #
def get_client():
    from google.oauth2.service_account import Credentials
    from google.cloud import bigquery

    sa_path = os.path.join(project_dir, 'service_account.json')
    if not os.path.exists(sa_path):
        print(f"ERROR: service_account.json not found at {sa_path}")
        sys.exit(1)

    creds = Credentials.from_service_account_file(
        sa_path, scopes=['https://www.googleapis.com/auth/bigquery']
    )
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
    """Pipe-join every matching child's text (multi-valued scalar fields)."""
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
    """Parse a feed date string to a UTC ISO-8601 string (BQ TIMESTAMP-ready), or None."""
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
    """Flatten the nested <salary> element to the 5 standard fields."""
    return {
        'salary_min': parse_float(get_nested_text(job_el, 'salary', 'salary_min')),
        'salary_max': parse_float(get_nested_text(job_el, 'salary', 'salary_max')),
        'salary_exact': parse_float(get_nested_text(job_el, 'salary', 'salary_exact')),
        'salary_currency': get_nested_text(job_el, 'salary', 'salary_currency'),
        'salary_type': get_nested_text(job_el, 'salary', 'salary_type'),
    }


# --------------------------------------------------------------------------- #
# Location helpers — every feed -> a list of location structs
# --------------------------------------------------------------------------- #
LOC_KEYS = ('country', 'region', 'postcode', 'city', 'street', 'formatted_address')


def _location_struct(loc_el):
    return {
        'country': get_text(loc_el, 'country'),
        'region': get_text(loc_el, 'region'),
        'postcode': get_text(loc_el, 'postal_code'),   # feed tag is <postal_code>
        'city': get_text(loc_el, 'city'),
        'street': get_text(loc_el, 'street'),
        'formatted_address': get_text(loc_el, 'formatted_address'),
    }


def parse_locations(job_el):
    """<locations><location>... -> list of structs (Appcast/ATS/Scrape/Backfill)."""
    container = job_el.find('locations')
    if container is None:
        return []
    return [_location_struct(loc) for loc in container.findall('location')]


def parse_cs_locations(job_el):
    """<multi_locations><item>free text</item>... -> structs with formatted_address only."""
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
# Per-feed parsers — return list[dict] of native (raw) fields + locations[]
# --------------------------------------------------------------------------- #
def parse_appcast(jobs):
    out = []
    for j in jobs:
        out.append({
            'entity_id': get_text(j, 'id'),
            'external_id': get_text(j, 'external_id'),
            'title': get_text(j, 'title'),
            'company': get_text(j, 'company'),
            'organization_id': get_text(j, 'organization_id'),
            'occupation': get_text(j, 'occupation'),
            'employment_type': get_all_text(j, 'employment_type'),
            'date_posted': parse_dt(get_text(j, 'date')),
            'date_end': parse_dt(get_text(j, 'date_end')),
            'remote_option': get_text(j, 'remote_option'),
            'workflow_state': get_text(j, 'workflow_state'),
            'application_workflow': get_text(j, 'application_workflow'),
            'application_information': get_text(j, 'application_information'),
            'logo_url': get_text(j, 'logo_url'),
            'url': get_text(j, 'url'),
            'locations': parse_locations(j),
        })
    return out


def parse_scrape(jobs):
    """Also used for the Backfill feed (identical shape)."""
    out = []
    for j in jobs:
        rec = {
            'external_id': get_text(j, 'id'),
            'entity_id': None,
            'title': get_text(j, 'title'),
            'url': get_text(j, 'url'),
            'apply_url': get_text(j, 'apply_url'),
            'job_location': get_text(j, 'job_location'),
            'organization_id': get_text(j, 'organization_id'),
            'organization_name': get_text(j, 'organization_name'),
            'organization_type': get_text(j, 'organization_type'),
            'occupation': get_text(j, 'occupation'),
            'category': get_text(j, 'category'),
            'contract_type': get_text(j, 'contract_type'),
            'workplace': get_text(j, 'workplace'),
            'working_pattern': get_text(j, 'working_pattern'),
            'working_hours': get_text(j, 'working_hours'),
            'salary_free_text': get_text(j, 'salary_free_text'),
            'salary_range_from': get_text(j, 'salary_range_from'),
            'salary_range_to': get_text(j, 'salary_range_to'),
            'reference': get_text(j, 'reference'),
            'old_account_id': get_text(j, 'old_account_id'),
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
            'external_id': get_text(j, 'id'),
            'entity_id': None,
            'jobiqo_org_id': get_text(j, 'jobiqo_id'),   # CS <jobiqo_id> = department org id
            'title': get_text(j, 'title'),
            'url': get_text(j, 'url'),
            'apply_url': get_text(j, 'apply_url'),
            'location': get_text(j, 'location'),          # free-text summary of all sites
            'occupation': get_text(j, 'occupation'),
            'category': get_text(j, 'category'),
            'contract_type': get_text(j, 'contract_type'),
            'workplace': get_text(j, 'workplace'),
            'working_pattern': get_text(j, 'working_pattern'),
            'salary_free_text': get_text(j, 'salary_free_text'),
            'reference': get_text(j, 'reference'),
            'frontends': get_text(j, 'frontends'),
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
            'external_id': get_text(j, 'id'),
            'entity_id': None,
            'title': get_text(j, 'title'),
            'url': get_text(j, 'url'),
            'apply_url': get_text(j, 'apply_url'),
            'job_location': get_text(j, 'job_location'),
            'organization_id': get_text(j, 'organization_id'),
            'organization_name': get_text(j, 'organization_name'),
            'organization_type': get_text(j, 'organization_type'),
            'occupation': get_text(j, 'occupation'),
            'category': get_text(j, 'category'),
            'contract_type': get_text(j, 'contract_type'),
            'workplace': get_text(j, 'workplace'),
            'working_pattern': get_text(j, 'working_pattern'),
            'working_hours': get_text(j, 'working_hours'),
            'working_hours_per': get_text(j, 'working_hours_per'),
            'working_hours_free_text': get_text(j, 'working_hours_free_text'),
            'salary_free_text': get_text(j, 'salary_free_text'),
            'salary_range_from': get_text(j, 'salary_range_from'),
            'salary_range_to': get_text(j, 'salary_range_to'),
            'reference': get_text(j, 'reference'),
            'old_vacancy_id': get_text(j, 'old_vacancy_id'),
            'old_account_id': get_text(j, 'old_account_id'),
            'frontends': get_text(j, 'frontends'),
            'internal_external': get_text(j, 'internal_external'),
            'application_method': get_text(j, 'application_method'),
            'crb_check': get_text(j, 'crb_check'),
            'created': parse_dt(get_text(j, 'created')),
            'updated': parse_dt(get_text(j, 'updated')),
            'start_date': parse_dt(get_text(j, 'start_date')),
            'close_date': parse_dt(get_text(j, 'close_date')),
            'locations': parse_locations(j),
        }
        rec.update(salary_fields(j))
        out.append(rec)
    return out


PARSERS = {
    "Appcast": parse_appcast,
    "Scrape": parse_scrape,
    "Civil Service": parse_civil_service,
    "ATS": parse_ats,
    "Backfill": parse_scrape,   # identical shape to Scrape
}


# --------------------------------------------------------------------------- #
# Explicit per-feed schemas (controls types + partitioning/clustering on create)
# --------------------------------------------------------------------------- #
def _schema():
    from google.cloud import bigquery
    S = lambda n, t='STRING': bigquery.SchemaField(n, t)
    TS, F = 'TIMESTAMP', 'FLOAT64'

    locations = bigquery.SchemaField('locations', 'RECORD', mode='REPEATED', fields=[
        S('country'), S('region'), S('postcode'), S('city'), S('street'), S('formatted_address'),
    ])
    meta = [S('feed_name'), bigquery.SchemaField('ingestion_date', 'DATE'), S('ingested_at', TS)]
    salary = [S('salary_min', F), S('salary_max', F), S('salary_exact', F),
              S('salary_currency'), S('salary_type')]

    scrape_like = [
        S('external_id'), S('entity_id'), S('title'), S('url'), S('apply_url'),
        S('job_location'), S('organization_id'), S('organization_name'), S('organization_type'),
        S('occupation'), S('category'), S('contract_type'), S('workplace'),
        S('working_pattern'), S('working_hours'), S('salary_free_text'),
        S('salary_range_from'), S('salary_range_to'),
    ] + salary + [
        S('reference'), S('old_account_id'), S('frontends'),
        S('start_date', TS), S('close_date', TS), locations,
    ] + meta

    return {
        "Appcast": [
            S('entity_id'), S('external_id'), S('title'), S('company'), S('organization_id'),
            S('occupation'), S('employment_type'), S('date_posted', TS), S('date_end', TS),
            S('remote_option'), S('workflow_state'), S('application_workflow'),
            S('application_information'), S('logo_url'), S('url'), locations,
        ] + meta,
        "Scrape": scrape_like,
        "Backfill": scrape_like,
        "Civil Service": [
            S('external_id'), S('entity_id'), S('jobiqo_org_id'), S('title'), S('url'),
            S('apply_url'), S('location'), S('occupation'), S('category'),
            S('contract_type'), S('workplace'), S('working_pattern'), S('salary_free_text'),
        ] + salary + [
            S('reference'), S('frontends'), S('start_date', TS), S('close_date', TS), locations,
        ] + meta,
        "ATS": [
            S('external_id'), S('entity_id'), S('title'), S('url'), S('apply_url'),
            S('job_location'), S('organization_id'), S('organization_name'), S('organization_type'),
            S('occupation'), S('category'), S('contract_type'), S('workplace'),
            S('working_pattern'), S('working_hours'), S('working_hours_per'),
            S('working_hours_free_text'), S('salary_free_text'),
            S('salary_range_from'), S('salary_range_to'),
        ] + salary + [
            S('reference'), S('old_vacancy_id'), S('old_account_id'), S('frontends'),
            S('internal_external'), S('application_method'), S('crb_check'),
            S('created', TS), S('updated', TS), S('start_date', TS), S('close_date', TS), locations,
        ] + meta,
    }


# --------------------------------------------------------------------------- #
# Fetch / build / write
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


def build_rows(records, feed_name, today_str, now_str):
    """Stamp each parsed record with feed/ingestion metadata. JSON-ready dicts."""
    for r in records:
        r['feed_name'] = feed_name
        r['ingestion_date'] = today_str   # 'YYYY-MM-DD' -> BQ DATE
        r['ingested_at'] = now_str        # ISO-8601 UTC -> BQ TIMESTAMP
    return records


def write_bronze(client, feed_name, rows, schema, today):
    from google.cloud import bigquery
    from google.api_core.exceptions import NotFound

    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{TABLE_NAMES[feed_name]}"

    # Idempotent re-run: clear today's partition first if the table already exists.
    try:
        client.get_table(table_id)
        client.query(
            f"DELETE FROM `{table_id}` WHERE ingestion_date = DATE('{today.isoformat()}')"
        ).result()
    except NotFound:
        pass  # first run — the load creates the table

    job_config = bigquery.LoadJobConfig(
        write_disposition='WRITE_APPEND',
        schema=schema,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field='ingestion_date'
        ),
        clustering_fields=['external_id'],
    )
    print(f"    writing {len(rows):,} rows -> {TABLE_NAMES[feed_name]}...", end=' ', flush=True)
    client.load_table_from_json(rows, table_id, job_config=job_config).result()
    print("OK")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _pct(num, den):
    return f"{(num / den * 100):.1f}%" if den else "n/a"


def main():
    parser = argparse.ArgumentParser(description='Bronze feed ingest into JPD.t01_feed_*')
    parser.add_argument('--dry-run', action='store_true',
                        help='Fetch + parse + report only; no BigQuery writes')
    args = parser.parse_args()

    start = datetime.now()
    today = date.today()
    today_str = today.isoformat()
    now_str = datetime.now(timezone.utc).isoformat()
    print("Bronze Feed Ingest -> JPD")
    print(f"Started: {start:%Y-%m-%d %H:%M:%S}  (ingestion_date={today_str})")

    schemas = _schema()
    client = None if args.dry_run else get_client()

    frames = {}
    print("\nFetching + parsing feeds...")
    for feed_name, url in FEEDS.items():
        jobs = fetch_feed(feed_name, url)
        records = PARSERS[feed_name](jobs)
        frames[feed_name] = build_rows(records, feed_name, today_str, now_str)

    # --- Report: row counts, entity_id linkage, location coverage ---
    print(f"\n{'='*72}\nRow counts | entity_id linkage | location coverage\n{'='*72}")
    appcast_ids = {r['external_id'] for r in frames.get('Appcast', []) if r.get('external_id')}
    for feed_name, rows in frames.items():
        n = len(rows)
        loc_rows = sum(1 for r in rows if r['locations'])
        total_locs = sum(len(r['locations']) for r in rows)
        max_locs = max((len(r['locations']) for r in rows), default=0)
        loc_str = (f"locs: {loc_rows:,} rows have any, {total_locs:,} total, max {max_locs}/vac")
        if feed_name == 'Appcast':
            ent = sum(1 for r in rows if r.get('entity_id'))
            print(f"  {feed_name:14s} {n:6,} rows | entity_id: {ent:,} ({_pct(ent, n)}) | {loc_str}")
        elif n:
            matched = sum(1 for r in rows if r.get('external_id') in appcast_ids)
            print(f"  {feed_name:14s} {n:6,} rows | in Appcast: {matched:,} ({_pct(matched, n)}) | {loc_str}")
        else:
            print(f"  {feed_name:14s} {n:6,} rows")

    if args.dry_run:
        print(f"\n[DRY RUN] No writes. Parsed {sum(len(d) for d in frames.values()):,} rows total.")
        print(f"Completed in {(datetime.now() - start).total_seconds():.0f}s")
        return

    print(f"\n{'='*72}\nWriting Bronze tables\n{'='*72}")
    for feed_name, rows in frames.items():
        if not rows:
            print(f"  {feed_name}: 0 rows — skipped")
            continue
        try:
            write_bronze(client, feed_name, rows, schemas[feed_name], today)
        except Exception as e:
            print(f"  {feed_name}: FAILED — {type(e).__name__}: {e}")
            if 'Not found: Dataset' in str(e) or 'notFound' in str(e):
                print(f"    -> Create the {BQ_DATASET} dataset (EU) first, then re-run.")

    print(f"\n{'='*72}\nCompleted in {(datetime.now() - start).total_seconds():.0f}s")


if __name__ == '__main__':
    main()
