#!/usr/bin/env python3
"""Historic vacancy backfill — import 4 CSVs from Jobiqo into JPD Bronze.

Each row is imported into TWO tables (mirrors the live-feed pattern):
  - Its source-feed t01_feed_* table (ATS / Scrape / Civil Service / Backfill)
  - t01_feed_appcast (registry overlay)

Filter: workflow_state NOT IN ('', 'published'). The user removed published
rows on the export side (those are already in the live feed); blank rows
are deletion artifacts from that step.

Schema setup (idempotent):
  - Adds jgp_external_vacancy_id STRING to all 5 t01_feed_* tables.
  - Backfills t01_feed_ats.jgp_external_vacancy_id from existing old_vacancy_id
    so live ATS rows align with the historic ones (same identifier).

Dates: CSV dates are DD/MM/YYYY HH:MM → parsed to UTC TIMESTAMP.
  - first_seen = Original publishing date (handles re-listings — earliest known date)
  - last_seen  = COALESCE(expiration_date, Original publishing date)
  - last_updated = run timestamp (we updated the record now, even with old data)

Locations: "Region, City, Country | Region, City, Country | ..."
  → ARRAY<STRUCT<country, region, postcode, city, street, formatted_address>>
  Dedup within vacancy. Empty tokens become NULL fields (e.g. ", , GB" → country only).

MERGE semantics:
  - first_seen: take EARLIER of T.first_seen and S.first_seen (push it back if historic predates)
  - last_seen:  take LATER of T.last_seen and S.last_seen (don't overwrite a live row with a past date)
  - last_updated: bump only if content_hash changed

Usage:
    venv/bin/python scripts/00_import_historic.py --dry-run
    venv/bin/python scripts/00_import_historic.py             # all 4 files
    venv/bin/python scripts/00_import_historic.py --files ATS Scrape
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

# CSV `description` can exceed default csv field-size limit
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"
BQ_DATASET = "JPD"

FILES = {
    "ATS":           "historic_ats.csv",
    "Scrape":        "historic_scrape.csv",
    "Civil Service": "historic_civil_service.csv",
    "Backfill":      "historic_backfill.csv",
}

SOURCE_TABLES = {
    "ATS": "t01_feed_ats", "Scrape": "t01_feed_scrape",
    "Civil Service": "t01_feed_civil_service", "Backfill": "t01_feed_backfill",
}
APPCAST_TABLE = "t01_feed_appcast"

ALL_T01_TABLES = list(SOURCE_TABLES.values()) + [APPCAST_TABLE]


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
# Parsers
# --------------------------------------------------------------------------- #
def parse_dt(s):
    """DD/MM/YYYY HH:MM → UTC ISO string, or None."""
    if not s or not s.strip():
        return None
    try:
        dt = datetime.strptime(s.strip(), '%d/%m/%Y %H:%M')
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


def parse_float(s):
    if not s or not s.strip():
        return None
    try:
        return float(s.strip())
    except (ValueError, TypeError):
        return None


def parse_locations(loc_str):
    """'Region, City, Country | Region, City, Country' → list of structs.
    Dedup within vacancy. Empty tokens → NULL field. Skip wholly-empty entries."""
    if not loc_str or not loc_str.strip():
        return []
    seen = set()
    out = []
    for part in loc_str.split('|'):
        part = part.strip()
        if not part:
            continue
        tokens = [t.strip() for t in part.split(',')]
        while len(tokens) < 3:
            tokens.append('')
        region = tokens[0] or None
        city = tokens[1] or None
        country = tokens[2] or None
        if not any((region, city, country)):
            continue
        key = (country, region, city)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            'country': country, 'region': region, 'postcode': None,
            'city': city, 'street': None, 'formatted_address': None,
        })
    return out


def keep_row(r):
    """Filter: drop blank/published rows."""
    ws = r.get('workflow_state', '')
    return bool(ws) and ws != 'published'


# --------------------------------------------------------------------------- #
# Row mappers — CSV row → target table row
# --------------------------------------------------------------------------- #
def _common_dates(r):
    """Pick first_seen and last_seen for the historic row.

    last_seen MUST be in the past — if expiration_date is in the future
    (some historic rows are scheduled to expire years out), fall back to
    publishing_date so MAX(last_seen) per feed isn't poisoned by a future
    date (which would break is_live derivation in t02).
    """
    first_seen = parse_dt(r.get('Original publishing date', '')) or parse_dt(r.get('publishing_date', ''))
    exp = parse_dt(r.get('expiration_date', ''))
    pub = parse_dt(r.get('publishing_date', ''))
    now_iso = datetime.now(timezone.utc).isoformat()
    if exp and exp < now_iso:
        last_seen = exp
    else:
        last_seen = pub or first_seen
    return first_seen, last_seen


def map_to_source(feed_name, r):
    """Project CSV row to source-feed table shape (per-feed schema)."""
    first_seen, last_seen = _common_dates(r)
    jgp_id = (r.get('jgp_external_vacancy_id') or '').strip() or None

    if feed_name == 'Civil Service':
        # CS schema: jobiqo_org_id, no organization_name/type
        return {
            'external_id': r.get('External ID') or None,
            'entity_id': r.get('job_id') or None,
            'jobiqo_org_id': r.get('organization_id') or None,
            'title': r.get('title') or None,
            'url': r.get('job_url') or None,
            'apply_url': r.get('application_link') or None,
            'location': None,  # CS schema has singular STRING; CSV doesn't carry equivalent
            'occupation': None,  # narrow — CSV only has broad (goes in category)
            'category': r.get('occupational_fields') or None,
            'contract_type': None,
            'workplace': None,
            'working_pattern': r.get('employment_type') or None,
            'salary_free_text': None,
            'salary_min': parse_float(r.get('min_salary', '')),
            'salary_max': parse_float(r.get('max_salary', '')),
            'salary_exact': None,
            'salary_currency': r.get('currency_code') or None,
            'salary_type': r.get('salary_unit') or None,
            'reference': None,
            'frontends': None,
            'start_date': parse_dt(r.get('publishing_date', '')),
            'close_date': parse_dt(r.get('expiration_date', '')),
            'locations': parse_locations(r.get('locations', '')),
            'jgp_external_vacancy_id': jgp_id,
            '_first_seen': first_seen,
            '_last_seen': last_seen,
        }

    # ATS / Scrape / Backfill share organization_name/_type and many fields
    base = {
        'external_id': r.get('External ID') or None,
        'entity_id': r.get('job_id') or None,
        'title': r.get('title') or None,
        'url': r.get('job_url') or None,
        'apply_url': r.get('application_link') or None,
        'job_location': None,
        'organization_id': r.get('organization_id') or None,
        'organization_name': r.get('organization_profile_name') or None,
        'organization_type': r.get('Employer type (Industry)') or None,
        'occupation': None,  # narrow — CSV broad goes in category
        'category': r.get('occupational_fields') or None,
        'contract_type': None,
        'workplace': None,
        'working_pattern': r.get('employment_type') or None,
        'working_hours': None,
        'salary_free_text': None,
        'salary_range_from': None,
        'salary_range_to': None,
        'salary_min': parse_float(r.get('min_salary', '')),
        'salary_max': parse_float(r.get('max_salary', '')),
        'salary_exact': None,
        'salary_currency': r.get('currency_code') or None,
        'salary_type': r.get('salary_unit') or None,
        'reference': None,
        'frontends': None,
        'start_date': parse_dt(r.get('publishing_date', '')),
        'close_date': parse_dt(r.get('expiration_date', '')),
        'locations': parse_locations(r.get('locations', '')),
        'jgp_external_vacancy_id': jgp_id,
        '_first_seen': first_seen,
        '_last_seen': last_seen,
    }
    if feed_name == 'ATS':
        base.update({
            'working_hours_per': None,
            'working_hours_free_text': None,
            'old_vacancy_id': jgp_id,        # same value as jgp_external_vacancy_id
            'old_account_id': None,
            'internal_external': None,
            'application_method': None,
            'crb_check': None,
            'created': None,
            'updated': None,
        })
    else:  # Scrape / Backfill
        base.update({'old_account_id': None})
    return base


def map_to_appcast(r):
    """Project CSV row to t01_feed_appcast shape."""
    first_seen, last_seen = _common_dates(r)
    return {
        'entity_id': r.get('job_id') or None,
        'external_id': r.get('External ID') or None,
        'title': r.get('title') or None,
        'company': r.get('organization_profile_name') or None,
        'organization_id': r.get('organization_id') or None,
        'occupation': r.get('occupational_fields') or None,
        'employment_type': r.get('employment_type') or None,
        'date_posted': parse_dt(r.get('publishing_date', '')),
        'date_end': parse_dt(r.get('expiration_date', '')),
        'remote_option': None,
        'workflow_state': r.get('workflow_state') or None,
        'application_workflow': r.get('application_workflow') or None,
        'application_information': None,
        'logo_url': r.get('Logo') or None,
        'url': r.get('job_url') or None,
        'locations': parse_locations(r.get('locations', '')),
        'jgp_external_vacancy_id': (r.get('jgp_external_vacancy_id') or '').strip() or None,
        '_first_seen': first_seen,
        '_last_seen': last_seen,
    }


# --------------------------------------------------------------------------- #
# Schema additions (idempotent)
# --------------------------------------------------------------------------- #
def alter_add_jgp_id(client):
    print("Ensuring jgp_external_vacancy_id column on all t01 tables...")
    for t in ALL_T01_TABLES:
        sql = (f"ALTER TABLE `{BQ_PROJECT}.{BQ_DATASET}.{t}` "
               f"ADD COLUMN IF NOT EXISTS jgp_external_vacancy_id STRING")
        client.query(sql).result()
        print(f"  ALTER OK: {t}")


def backfill_ats_jgp_from_old_vacancy_id(client):
    sql = f"""
    UPDATE `{BQ_PROJECT}.{BQ_DATASET}.t01_feed_ats`
    SET jgp_external_vacancy_id = old_vacancy_id
    WHERE jgp_external_vacancy_id IS NULL AND old_vacancy_id IS NOT NULL
    """
    job = client.query(sql)
    job.result()
    n = job.num_dml_affected_rows or 0
    print(f"Backfilled live ATS rows: {n:,} (jgp_external_vacancy_id = old_vacancy_id)")


# --------------------------------------------------------------------------- #
# Staging schemas (mirror 01_sync_bronze_feeds.py content schema)
# --------------------------------------------------------------------------- #
def _location_field():
    from google.cloud import bigquery
    return bigquery.SchemaField('locations', 'RECORD', mode='REPEATED', fields=[
        bigquery.SchemaField('country', 'STRING'),
        bigquery.SchemaField('region', 'STRING'),
        bigquery.SchemaField('postcode', 'STRING'),
        bigquery.SchemaField('city', 'STRING'),
        bigquery.SchemaField('street', 'STRING'),
        bigquery.SchemaField('formatted_address', 'STRING'),
    ])


def _ts_meta_fields():
    from google.cloud import bigquery
    return [bigquery.SchemaField('_first_seen', 'TIMESTAMP'),
            bigquery.SchemaField('_last_seen',  'TIMESTAMP')]


def source_schema(feed_name):
    from google.cloud import bigquery
    S = lambda n, t='STRING': bigquery.SchemaField(n, t)
    F, TS = 'FLOAT64', 'TIMESTAMP'
    salary = [S('salary_min', F), S('salary_max', F), S('salary_exact', F),
              S('salary_currency'), S('salary_type')]

    if feed_name == 'Civil Service':
        cols = [
            S('external_id'), S('entity_id'), S('jobiqo_org_id'), S('title'),
            S('url'), S('apply_url'), S('location'), S('occupation'), S('category'),
            S('contract_type'), S('workplace'), S('working_pattern'),
            S('salary_free_text'),
        ] + salary + [
            S('reference'), S('frontends'),
            S('start_date', TS), S('close_date', TS), _location_field(),
            S('jgp_external_vacancy_id'),
        ]
    else:
        cols = [
            S('external_id'), S('entity_id'), S('title'), S('url'), S('apply_url'),
            S('job_location'), S('organization_id'), S('organization_name'),
            S('organization_type'), S('occupation'), S('category'),
            S('contract_type'), S('workplace'), S('working_pattern'),
            S('working_hours'), S('salary_free_text'), S('salary_range_from'),
            S('salary_range_to'),
        ] + salary + [S('reference')]
        if feed_name == 'ATS':
            cols += [
                S('old_vacancy_id'), S('old_account_id'), S('frontends'),
                S('internal_external'), S('application_method'), S('crb_check'),
                S('created', TS), S('updated', TS),
                S('working_hours_per'), S('working_hours_free_text'),
            ]
        else:  # Scrape / Backfill
            cols += [S('old_account_id'), S('frontends')]
        cols += [S('start_date', TS), S('close_date', TS), _location_field(),
                 S('jgp_external_vacancy_id')]
    return cols + _ts_meta_fields()


def appcast_schema():
    from google.cloud import bigquery
    S = lambda n, t='STRING': bigquery.SchemaField(n, t)
    return [
        S('entity_id'), S('external_id'), S('title'), S('company'),
        S('organization_id'), S('occupation'), S('employment_type'),
        S('date_posted', 'TIMESTAMP'), S('date_end', 'TIMESTAMP'),
        S('remote_option'), S('workflow_state'), S('application_workflow'),
        S('application_information'), S('logo_url'), S('url'),
        _location_field(), S('jgp_external_vacancy_id'),
    ] + _ts_meta_fields()


# --------------------------------------------------------------------------- #
# MERGE
# --------------------------------------------------------------------------- #
def _content_cols_from_schema(schema):
    """Column names from schema EXCLUDING the _first_seen/_last_seen meta cols."""
    return [f.name for f in schema if f.name not in ('_first_seen', '_last_seen')]


def run_merge(client, target_table, staging_id, content_cols, match_sql, dedup_key,
              stable_col, run_ts):
    """Idempotent upsert with per-row first_seen/last_seen from staging.

    first_seen: take the EARLIEST of T.first_seen and S._first_seen
    last_seen:  take the LATEST   of T.last_seen  and S._last_seen
    last_updated: bump only when content changes
    """
    ts = f'TIMESTAMP("{run_ts}")'
    hash_struct = ', '.join(content_cols)
    set_cols = [c for c in content_cols if c != stable_col]
    set_clause = ',\n      '.join(f'T.{c} = S.{c}' for c in set_cols)
    insert_cols = content_cols + ['content_hash', 'first_seen', 'last_seen', 'last_updated']
    insert_vals = [f'S.{c}' for c in content_cols] + [
        'S.content_hash', 'S._first_seen', 'S._last_seen', ts,
    ]
    target_id = f"{BQ_PROJECT}.{BQ_DATASET}.{target_table}"

    sql = f"""
    MERGE `{target_id}` T
    USING (
      SELECT *, FARM_FINGERPRINT(TO_JSON_STRING(STRUCT({hash_struct}))) AS content_hash
      FROM `{staging_id}`
      QUALIFY ROW_NUMBER() OVER (PARTITION BY {dedup_key} ORDER BY {dedup_key}) = 1
    ) S
    ON {match_sql}
    WHEN MATCHED THEN UPDATE SET
      {set_clause},
      first_seen = COALESCE(LEAST(T.first_seen, S._first_seen), T.first_seen, S._first_seen),
      last_seen  = COALESCE(GREATEST(T.last_seen, S._last_seen), T.last_seen, S._last_seen),
      last_updated = IF(T.content_hash != S.content_hash, {ts}, T.last_updated),
      content_hash = S.content_hash
    WHEN NOT MATCHED THEN INSERT ({', '.join(insert_cols)})
    VALUES ({', '.join(insert_vals)})
    """
    job = client.query(sql)
    job.result()
    client.query(f"DROP TABLE `{staging_id}`").result()
    return job.num_dml_affected_rows or 0


def load_staging(client, table_suffix, rows, schema):
    from google.cloud import bigquery
    staging_id = f"{BQ_PROJECT}.{BQ_DATASET}.{table_suffix}_staging_historic"
    job_config = bigquery.LoadJobConfig(write_disposition='WRITE_TRUNCATE', schema=schema)
    client.load_table_from_json(rows, staging_id, job_config=job_config).result()
    return staging_id


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _pct(n, d):
    return f"{(n/d*100):.1f}%" if d else "n/a"


def process_file(client, feed_name, run_ts, dry_run):
    path = os.path.join(project_dir, FILES[feed_name])
    if not os.path.exists(path):
        print(f"  SKIP {feed_name}: file not found ({path})")
        return 0, 0

    with open(path, 'r', encoding='utf-8') as f:
        rows = [r for r in csv.DictReader(f) if keep_row(r)]
    if not rows:
        print(f"  {feed_name}: 0 rows after filter — skipped")
        return 0, 0
    n_locs = sum(1 for r in rows if r.get('locations'))
    n_exp  = sum(1 for r in rows if r.get('expiration_date'))
    n_jgp  = sum(1 for r in rows if r.get('jgp_external_vacancy_id'))
    print(f"  {feed_name}: {len(rows):,} rows kept "
          f"| locs {_pct(n_locs, len(rows))} | exp_date {_pct(n_exp, len(rows))} "
          f"| jgp_id {_pct(n_jgp, len(rows))}")

    src_rows = [map_to_source(feed_name, r) for r in rows]
    app_rows = [map_to_appcast(r) for r in rows]

    if dry_run:
        # Show a sample of parsed locations from the first row that has them
        for r in src_rows:
            if r.get('locations'):
                print(f"    sample parsed locations (first): {r['locations'][:2]}")
                break
        return len(src_rows), len(app_rows)

    src_table = SOURCE_TABLES[feed_name]
    src_schema = source_schema(feed_name)
    src_cols = _content_cols_from_schema(src_schema)
    staging_id = load_staging(client, src_table, src_rows, src_schema)
    n_src = run_merge(client, src_table, staging_id, src_cols,
                      match_sql="T.external_id = S.external_id",
                      dedup_key="external_id", stable_col="external_id",
                      run_ts=run_ts)
    print(f"    {src_table:28s} MERGE OK ({n_src:,} rows affected)")

    app_schema = appcast_schema()
    app_cols = _content_cols_from_schema(app_schema)
    staging_id = load_staging(client, APPCAST_TABLE, app_rows, app_schema)
    n_app = run_merge(client, APPCAST_TABLE, staging_id, app_cols,
                      match_sql="T.entity_id = S.entity_id OR T.external_id = S.external_id",
                      dedup_key="entity_id", stable_col="entity_id",
                      run_ts=run_ts)
    print(f"    {APPCAST_TABLE:28s} MERGE OK ({n_app:,} rows affected)")
    return n_src, n_app


def main():
    ap = argparse.ArgumentParser(description='Historic vacancy backfill into JPD Bronze')
    ap.add_argument('--dry-run', action='store_true',
                    help='Parse + report; do NOT alter, backfill, or write to BQ')
    ap.add_argument('--files', nargs='+', default=list(FILES.keys()),
                    choices=list(FILES.keys()),
                    help='Which feeds to process (default: all 4)')
    ap.add_argument('--skip-alter', action='store_true',
                    help='Skip ALTER TABLE + ATS backfill (use if you ran them already)')
    args = ap.parse_args()

    start = datetime.now()
    run_ts = datetime.now(timezone.utc).isoformat()
    print(f"Historic backfill -> JPD\nStarted: {start:%Y-%m-%d %H:%M:%S}")

    client = None if args.dry_run else get_client()

    if not args.dry_run and not args.skip_alter:
        alter_add_jgp_id(client)
        backfill_ats_jgp_from_old_vacancy_id(client)
        print()

    print(f"Processing {len(args.files)} file(s): {', '.join(args.files)}")
    total_src = total_app = 0
    for fn in args.files:
        s, a = process_file(client, fn, run_ts, args.dry_run)
        total_src += s
        total_app += a

    print(f"\nTotal source-feed rows merged: {total_src:,}")
    print(f"Total Appcast rows merged:     {total_app:,}")
    print(f"Completed in {(datetime.now()-start).total_seconds():.0f}s")


if __name__ == '__main__':
    main()
