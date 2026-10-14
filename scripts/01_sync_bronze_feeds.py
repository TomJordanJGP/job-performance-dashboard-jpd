#!/usr/bin/env python3
"""Bronze ingest — upsert the job feeds into JPD.t01_feed_* current-state tables.

Before polling, the registry is reconciled with the private feed map (see
sync_feed_map): a feed new to the map is validated and added, a feed that left it
is retired, a retired feed that came back is reactivated. So every live feed is
ingested without anyone editing the registry.

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
  - Source feeds (ATS, Scrape, Civil Service, Backfill, ... and whatever the feed map
    adds) — <id> = hash (external_id), no entity_id.
The source feeds are mutually exclusive; Appcast overlays ~99% and supplies entity_id.

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
import re
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

REGISTRY_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.t00_feed_registry"
RUNS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.t00_feed_runs"

# Feeds are defined as rows in t00_feed_registry — the URL, target table, shape
# and dedupe priority all live there, NOT in this file. Adding a matched feed is
# a one-row INSERT into that table; no code change here. Parsers and schemas stay
# in code as a library keyed by the feed's `shape` (see PARSER_BY_SHAPE and
# _content_schema below) — the registry just says which shape each feed uses.
# Only a genuinely new XML layout needs a new parser + schema entry.

# Freshness preflight: a feed whose newest advertised posting date is older than
# this many days is flagged 'stale' (a warning — never aborts the run).
FRESHNESS_DAYS = 14


def merge_cfg(feed):
    """Per-feed MERGE config: the protected `stable` key (never overwritten on
    UPDATE so a site-created job's entity_id survives) and the target table
    clustering. The match/dedup logic lives in merge_passes() — Appcast needs
    two single-key passes, every other feed one."""
    if feed["feed_kind"] == "appcast":
        return dict(stable="entity_id", cluster=["entity_id", "external_id"])
    return dict(stable="external_id", cluster=["external_id"])


# Recency expression per feed shape — drives the deterministic dedup tiebreak
# (newest record wins when a feed broadcasts the same id twice in one poll).
RECENCY_BY_SHAPE = {
    "appcast": "COALESCE(date_posted, date_end)",
    "ats": "COALESCE(updated, created, start_date)",
    "scrape": "start_date",
    "civil_service": "start_date",
}


def merge_passes(feed):
    """Ordered MERGE passes. Appcast matched on a single OR-clause
    (entity_id OR external_id) can match one target row from TWO source rows,
    which BigQuery rejects — aborting the whole MERGE. Instead Appcast runs two
    disjoint single-key passes: entity_id for rows that carry it (≈all of them;
    the UPDATE then fills external_id), external_id for the rare residual. A
    single-key pass deduped on that same key can match at most one source row
    per target, so the abort can't happen. Source feeds: one external_id pass."""
    if feed["feed_kind"] == "appcast":
        return [
            dict(key="entity_id", match="T.entity_id = S.entity_id",
                 where="entity_id IS NOT NULL"),
            dict(key="external_id", match="T.external_id = S.external_id",
                 where="entity_id IS NULL AND external_id IS NOT NULL"),
        ]
    return [dict(key="external_id", match="T.external_id = S.external_id", where=None)]


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
    return bigquery.Client(credentials=creds, project=BQ_PROJECT, location="EU")


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
            'jgp_external_vacancy_id': get_text(j, 'old_vacancy_id'),  # JGP-side ID — same value as old_vacancy_id, exposed under its canonical name for downstream joins
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


# Parser library keyed by feed `shape` (from the registry). 'scrape' covers both
# the Scrape and Backfill feeds — identical XML layout.
PARSER_BY_SHAPE = {"appcast": parse_appcast, "scrape": parse_scrape,
                   "civil_service": parse_civil_service, "ats": parse_ats}


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
        "appcast": [
            S('entity_id'), S('external_id'), S('title'), S('company'), S('organization_id'),
            S('occupation'), S('employment_type'), S('date_posted', TS), S('date_end', TS),
            S('remote_option'), S('workflow_state'), S('application_workflow'),
            S('application_information'), S('logo_url'), S('url'), loc],
        "scrape": scrape_like,
        "civil_service": [
            S('external_id'), S('entity_id'), S('jobiqo_org_id'), S('title'), S('url'), S('apply_url'),
            S('location'), S('occupation'), S('category'), S('contract_type'), S('workplace'),
            S('working_pattern'), S('salary_free_text'),
        ] + salary + [S('reference'), S('frontends'), S('start_date', TS), S('close_date', TS), loc],
        "ats": [
            S('external_id'), S('entity_id'), S('title'), S('url'), S('apply_url'), S('job_location'),
            S('organization_id'), S('organization_name'), S('organization_type'), S('occupation'),
            S('category'), S('contract_type'), S('workplace'), S('working_pattern'), S('working_hours'),
            S('working_hours_per'), S('working_hours_free_text'), S('salary_free_text'),
            S('salary_range_from'), S('salary_range_to'),
        ] + salary + [S('reference'), S('old_vacancy_id'), S('jgp_external_vacancy_id'),
                      S('old_account_id'), S('frontends'),
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
def safe_error(e):
    """An exception summary that can't contain a URL: requests puts the URL in
    the message of most of its errors, and feed URLs are private."""
    status = getattr(getattr(e, 'response', None), 'status_code', None)
    return f"{type(e).__name__}" + (f" (HTTP {status})" if status else "")


def fetch_feed(feed_name, url):
    print(f"  Fetching {feed_name}...", end=' ', flush=True)
    try:
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        jobs = ET.fromstring(resp.content).findall('.//job')
        print(f"{len(jobs):,} jobs ({len(resp.content) / 1e6:.1f} MB)")
        return jobs
    except Exception as e:
        print(f"FAILED: {safe_error(e)}")
        return []


def ensure_target(client, feed, content_schema):
    from google.cloud import bigquery
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{feed['table_name']}"
    try:
        client.get_table(table_id)
    except Exception:
        table = bigquery.Table(table_id, schema=_target_schema(content_schema))
        table.clustering_fields = merge_cfg(feed)['cluster']
        client.create_table(table)
        print(f"    created {feed['table_name']}")
    return table_id


def load_staging(client, feed, rows, content_schema):
    from google.cloud import bigquery
    staging_id = f"{BQ_PROJECT}.{BQ_DATASET}.{feed['table_name']}_staging"
    job_config = bigquery.LoadJobConfig(write_disposition='WRITE_TRUNCATE', schema=content_schema)
    client.load_table_from_json(rows, staging_id, job_config=job_config).result()
    return staging_id


def run_merge(client, feed, target_id, staging_id, content_cols, run_ts):
    cfg = merge_cfg(feed)
    ts = f'TIMESTAMP("{run_ts}")'
    hash_struct = ', '.join(content_cols)
    recency = RECENCY_BY_SHAPE.get(feed['shape'], 'start_date')
    set_cols = [c for c in content_cols if c != cfg['stable']]
    set_clause = ',\n      '.join(f'T.{c} = S.{c}' for c in set_cols)
    insert_cols = content_cols + ['content_hash', 'first_seen', 'last_seen', 'last_updated']
    insert_vals = [f'S.{c}' for c in content_cols] + ['S.content_hash', ts, ts, ts]

    total = 0
    for p in merge_passes(feed):
        where = f"WHERE {p['where']}" if p.get('where') else ""
        # Dedup this pass's slice to one row per match key (newest wins via the
        # recency tiebreak), then MERGE on that single key — never an OR-clause,
        # so a target row can match at most one source row.
        sql = f"""
        MERGE `{target_id}` T
        USING (
          SELECT * EXCEPT(_rn),
                 FARM_FINGERPRINT(TO_JSON_STRING(STRUCT({hash_struct}))) AS content_hash
          FROM (
            SELECT *, ROW_NUMBER() OVER (
              PARTITION BY {p['key']} ORDER BY {recency} DESC) AS _rn
            FROM `{staging_id}`
            {where}
          )
          WHERE _rn = 1
        ) S
        ON {p['match']}
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
        total += job.num_dml_affected_rows or 0
    client.query(f"DROP TABLE `{staging_id}`").result()
    return total


def _pct(num, den):
    return f"{(num / den * 100):.1f}%" if den else "n/a"


def load_registry(client):
    """Active, non-excluded feeds from t00_feed_registry. The URL/table/shape/
    priority all live there, not in this file; sync_feed_map keeps the source
    rows in step with the feed map."""
    q = f"""
    SELECT feed_name, url, table_name, feed_kind, shape, org_id_column, priority
    FROM `{REGISTRY_TABLE}`
    WHERE active AND feed_kind IN ('source', 'appcast') AND NOT COALESCE(excluded, FALSE)
    ORDER BY feed_kind, priority
    """
    return [dict(r.items()) for r in client.query(q).result()]


def feed_max_date(shape, rows):
    """Newest advertised posting date (<= now) across a feed's parsed rows — the
    freshness signal. Future-dated values are ignored (a scheduled start_date is
    not evidence of a recent post; see lessons.md on future-date leaks)."""
    field = 'date_posted' if shape == 'appcast' else 'start_date'
    now = datetime.now(timezone.utc)
    best = None
    for r in rows:
        v = r.get(field)
        if not v:
            continue
        try:
            d = dateparser.parse(v)
        except Exception:
            continue
        if d is None:
            continue
        d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
        if d <= now and (best is None or d > best):
            best = d
    return best


def runs_schema():
    from google.cloud import bigquery
    S = lambda n, t='STRING': bigquery.SchemaField(n, t)
    return [
        S('run_ts', 'TIMESTAMP'), S('feed_name'), S('url'),
        S('jobs_fetched', 'INT64'), S('rows_merged', 'INT64'),
        S('max_feed_date', 'TIMESTAMP'), S('status'), S('message'),
    ]


def write_run_log(client, rows):
    """Append one row per (feed, run) to t00_feed_runs (load job, not streaming —
    no buffer lock, immediately queryable)."""
    from google.cloud import bigquery
    if not rows:
        return
    job_config = bigquery.LoadJobConfig(write_disposition='WRITE_APPEND', schema=runs_schema())
    client.load_table_from_json(rows, RUNS_TABLE, job_config=job_config).result()

# --------------------------------------------------------------------------- #
# Feed-map discovery
# --------------------------------------------------------------------------- #
# The feed map (its URL is the registry row with feed_kind = 'map') lists every
# feed JGP runs. Each run reconciles the registry with it before polling, matching
# on the exact URL:
#   - a map URL that no registry row has    -> validated, then added as an 'ats'
#     shape source feed (every map feed uses the ATS layout)
#   - an active source feed not in the map  -> retired: no longer polled, but its
#     Bronze table stays and t02 keeps its vacancies
#   - a retired source feed back in the map -> reactivated
# Rows with excluded = TRUE (the test feed, Tribepad) are a human override and are
# never touched, nor is the Appcast overlay (not in the map). Logs name feeds,
# never URLs: the map and the feed URLs are private.

# If more than this share of the active source feeds would retire in one run, the
# map is more likely truncated than the feeds gone: retire nothing that run.
MAX_RETIRE_SHARE = 0.5
# Labels t02 already uses for its own segments, and Bronze tables made by hand
# outside the registry — a new feed must not take either.
RESERVED_FEED_NAMES = {'appcast', 'self-service'}
RESERVED_TABLES = {'t01_feed_appcast', 't01_feed_selfservice'}


def parse_feed_map(content):
    """The feed map XML: <items><lastBuildDate/><item><name/><url/></item>...</items>
    -> [{'name', 'url'}], one per distinct URL, in map order."""
    items, seen = [], set()
    for it in ET.fromstring(content).findall('item'):
        url = (it.findtext('url') or '').strip()
        if url and url not in seen:
            seen.add(url)
            items.append({'name': (it.findtext('name') or '').strip() or 'Unnamed feed', 'url': url})
    return items


def plan_map_changes(map_items, registry_rows):
    """Pure: the registry changes that make the source feeds match the map.

    registry_rows is EVERY registry row (any feed_kind, excluded or not) as dicts
    with url, feed_kind, active, excluded — a URL any row already has is never
    re-added, which is what keeps excluded feeds out. Returns lists add (map
    items), retire, reactivate (registry rows) and held (retirements skipped
    because so many at once means a truncated map)."""
    known = {r['url'] for r in registry_rows if r['url']}
    in_map = {i['url'] for i in map_items}
    managed = [r for r in registry_rows if r['feed_kind'] == 'source' and not r['excluded']]
    retire = [r for r in managed if r['active'] and r['url'] not in in_map]
    n_active = sum(1 for r in managed if r['active'])
    held = []
    if retire and len(retire) > MAX_RETIRE_SHARE * n_active:
        held, retire = retire, []
    return {
        'add': [i for i in map_items if i['url'] not in known],
        'retire': retire,
        'reactivate': [r for r in managed if not r['active'] and r['url'] in in_map],
        'held': held,
    }


def slugify(name):
    """'Jgp Myjobscotland Feed' -> 'myjobscotland', 'Organic-1 feed' -> 'organic_1'."""
    s = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    s = re.sub(r'_feed$', '', re.sub(r'^jgp_', '', s))
    return s or 'feed'


def allocate_identity(name, taken_names, taken_tables):
    """A feed_name and table_name for a new feed that clash with nothing already
    used (or reserved), suffixing ' (2)' / '_2' as needed. Updates both sets so
    several feeds added in one run can't collide with each other."""
    feed_name, n = name, 2
    while feed_name.lower() in taken_names or feed_name.lower() in RESERVED_FEED_NAMES:
        feed_name, n = f"{name} ({n})", n + 1
    base = f"t01_feed_{slugify(name)}"
    table, n = base, 2
    while table in taken_tables or table in RESERVED_TABLES or f"{table}_staging" in taken_tables:
        table, n = f"{base}_{n}", n + 1
    taken_names.add(feed_name.lower())
    taken_tables.update({table, f"{table}_staging"})
    return feed_name, table


def announce(msg, level='warning'):
    """Print a registry change or problem; under GitHub Actions also annotate the
    run so it shows on the run summary."""
    print(f"  {msg}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{msg}")


def _registry_dml(client, sql, **params):
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter(k, 'INT64' if isinstance(v, int) else 'STRING', v)
        for k, v in params.items()])
    job = client.query(sql, job_config=cfg)
    job.result()
    return job.num_dml_affected_rows or 0


def sync_feed_map(client, dry_run):
    """Reconcile t00_feed_registry with the feed map (see the section comment).

    Returns (prefetched, planned, problems):
      prefetched  {feed_name: parsed rows} for feeds validated this run, so main()
                  doesn't download them a second time
      planned     registry dicts a dry run would add, so the preflight reports them
      problems    soft failures: the run still ingests every registered feed, then
                  exits 2 so the refresh is flagged
    A map that can't be fetched or lists nothing never changes the registry."""
    print("\nFeed map: reconciling the registry")
    rows = [dict(r.items()) for r in client.query(f"""
        SELECT feed_name, url, table_name, feed_kind, active, priority,
               COALESCE(excluded, FALSE) AS excluded
        FROM `{REGISTRY_TABLE}`""").result()]
    maps = [r for r in rows if r['feed_kind'] == 'map']
    if not maps:
        msg = "no feed map row in t00_feed_registry (feed_kind = 'map'); discovery skipped"
        announce(msg, 'error')
        return {}, [], [msg]
    maps = [r for r in maps if not r['excluded']]
    if not maps:
        print("  discovery paused (the feed map row is excluded)")
        return {}, [], []
    if len(maps) > 1:
        msg = f"{len(maps)} feed map rows in t00_feed_registry; expected one. Discovery skipped"
        announce(msg, 'error')
        return {}, [], [msg]

    try:
        resp = requests.get(maps[0]['url'], timeout=60)
        resp.raise_for_status()
        items = parse_feed_map(resp.content)
    except Exception as e:
        msg = f"feed map unavailable ({safe_error(e)}); registry left as is"
        announce(msg, 'error')
        return {}, [], [msg]
    if not items:
        msg = "feed map lists no feeds; registry left as is"
        announce(msg, 'error')
        return {}, [], [msg]

    plan = plan_map_changes(items, rows)
    print(f"  {len(items)} feeds in the map: {len(plan['add'])} new, "
          f"{len(plan['retire'])} to retire, {len(plan['reactivate'])} to reactivate")
    prefetched, planned, problems = {}, [], []
    verb = "would " if dry_run else ""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    if plan['held']:
        names = ', '.join(r['feed_name'] for r in plan['held'])
        msg = (f"feed map is missing {len(plan['held'])} active feeds at once ({names}); "
               f"looks truncated, so none were retired")
        announce(msg, 'error')
        problems.append(msg)

    if plan['add']:
        taken_names = {r['feed_name'].lower() for r in rows if r['feed_name']}
        taken_tables = {r.table_name for r in client.query(
            f"SELECT table_name FROM `{BQ_PROJECT}.{BQ_DATASET}.INFORMATION_SCHEMA.TABLES`").result()}
        for r in rows:
            if r['table_name']:
                taken_tables.update({r['table_name'], f"{r['table_name']}_staging"})
        priority = max((r['priority'] for r in rows
                        if r['feed_kind'] == 'source' and r['priority'] is not None), default=0)

    for item in plan['add']:
        # Validate before registering: a feed the ATS parser can't read, or whose
        # jobs lack an <id> (the MERGE key), would insert junk or duplicate rows.
        parsed = parse_ats(fetch_feed(item['name'], item['url']))
        no_id = sum(1 for r in parsed if not r['external_id'])
        if not parsed or no_id:
            why = "no jobs" if not parsed else f"{no_id} of {len(parsed)} jobs have no <id>"
            msg = f"new feed '{item['name']}' in the feed map not added: {why}"
            announce(msg, 'error')
            problems.append(msg)
            continue
        feed_name, table = allocate_identity(item['name'], taken_names, taken_tables)
        priority += 1
        feed = dict(feed_name=feed_name, url=item['url'], table_name=table, feed_kind='source',
                    shape='ats', org_id_column='organization_id', priority=priority)
        if not dry_run:
            # NOT EXISTS guards against two runs adding the same URL at once.
            _registry_dml(client, f"""
                INSERT INTO `{REGISTRY_TABLE}` (feed_name, url, table_name, feed_kind, shape,
                    org_id_column, priority, active, excluded, notes, added_at)
                SELECT @name, @url, @tbl, 'source', 'ats', 'organization_id', @prio, TRUE, FALSE,
                       @note, CURRENT_TIMESTAMP()
                FROM UNNEST([1])
                WHERE NOT EXISTS (SELECT 1 FROM `{REGISTRY_TABLE}` WHERE url = @url)""",
                name=feed_name, url=item['url'], tbl=table, prio=priority,
                note=f"Auto-added from the feed map {today}.")
        else:
            planned.append(feed)
        prefetched[feed_name] = parsed
        announce(f"feed map: {verb}add '{feed_name}' ({len(parsed):,} jobs) as {table}, priority {priority}")

    for r, active, note in ([(r, False, f"Retired {today}: not in the feed map.") for r in plan['retire']]
                            + [(r, True, f"Reactivated {today}: back in the feed map.") for r in plan['reactivate']]):
        if not dry_run:
            _registry_dml(client, f"""
                UPDATE `{REGISTRY_TABLE}`
                SET active = {'TRUE' if active else 'FALSE'},
                    notes = CONCAT(IFNULL(CONCAT(notes, ' '), ''), @note)
                WHERE url = @url AND feed_kind = 'source' AND NOT COALESCE(excluded, FALSE)""",
                url=r['url'], note=note)
        announce(f"feed map: {verb}{'reactivate' if active else 'retire'} '{r['feed_name']}'")

    if not (plan['add'] or plan['retire'] or plan['reactivate'] or plan['held']):
        print("  no changes")
    return prefetched, planned, problems


def main():
    ap = argparse.ArgumentParser(
        description='Bronze upsert ingest into JPD.t01_feed_* (registry-driven)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Fetch + parse + preflight + report only; no MERGE, no run-log write')
    ap.add_argument('--strict', action='store_true',
                    help='Abort before any MERGE if an active feed returned 0 jobs (likely dead URL)')
    args = ap.parse_args()

    start = datetime.now()
    run_ts = datetime.now(timezone.utc).isoformat()
    print("Bronze Feed Upsert -> JPD (registry-driven)")
    print(f"Started: {start:%Y-%m-%d %H:%M:%S}")

    content_schemas = _content_schema()
    client = get_client()                      # always — the feed list lives in BQ now
    prefetched, planned, map_problems = sync_feed_map(client, args.dry_run)
    feeds = load_registry(client) + planned    # planned is non-empty only in a dry run
    print(f"\nLoaded {len(feeds)} active feed(s) from t00_feed_registry"
          + (f" (+{len(planned)} a live run would add)" if planned else ""))
    if not feeds:
        print("No active feeds — nothing to do.")
        sys.exit(2 if map_problems else 0)

    # Fetch + parse every active feed
    frames = {}
    print("\nFetching + parsing feeds...")
    for feed in feeds:
        if feed['feed_name'] in prefetched:    # fetched + parsed while validating it
            frames[feed['feed_name']] = prefetched[feed['feed_name']]
            continue
        parser = PARSER_BY_SHAPE.get(feed['shape'])
        if parser is None:
            print(f"  {feed['feed_name']}: no parser for shape '{feed['shape']}' — skipped")
            frames[feed['feed_name']] = []
            continue
        frames[feed['feed_name']] = parser(fetch_feed(feed['feed_name'], feed['url']))

    # Preflight: status per feed + report
    print(f"\n{'='*72}\nPreflight | rows | entity_id / Appcast linkage | freshness\n{'='*72}")
    appcast_ids = set()
    for f in feeds:
        if f['feed_kind'] == 'appcast':
            appcast_ids = {r['external_id'] for r in frames[f['feed_name']] if r.get('external_id')}
            break
    now = datetime.now(timezone.utc)
    statuses = {}     # feed_name -> {status, message, max_date}
    for feed in feeds:
        name, rows = feed['feed_name'], frames[feed['feed_name']]
        n = len(rows)
        max_date = feed_max_date(feed['shape'], rows)
        if n == 0:
            status, msg = 'empty', 'feed returned 0 jobs (check URL?)'
        elif max_date is None:
            status, msg = 'ok', 'no usable posting date'
        elif (now - max_date).days > FRESHNESS_DAYS:
            status, msg = 'stale', f"newest post {(now - max_date).days}d old (> {FRESHNESS_DAYS}d)"
        else:
            status, msg = 'ok', ''
        statuses[name] = {'status': status, 'message': msg, 'max_date': max_date}

        locs = sum(len(r['locations']) for r in rows)
        fresh = max_date.strftime('%Y-%m-%d') if max_date else 'n/a'
        flag = '' if status == 'ok' else f"   <-- {status.upper()}: {msg}"
        if feed['feed_kind'] == 'appcast':
            ent = sum(1 for r in rows if r.get('entity_id'))
            link = f"entity_id {_pct(ent, n)}"
        else:
            matched = sum(1 for r in rows if r.get('external_id') in appcast_ids)
            link = f"in Appcast {_pct(matched, n)}"
        print(f"  {name:14s} {n:6,} rows | {link:18s} | newest {fresh} | locs {locs:,}{flag}")

    empty_feeds = [n for n, s in statuses.items() if s['status'] == 'empty']
    if args.strict and empty_feeds:
        print(f"\n[STRICT] Aborting — feed(s) returned 0 jobs: {', '.join(empty_feeds)}")
        if not args.dry_run:
            write_run_log(client, [{
                'run_ts': run_ts, 'feed_name': f['feed_name'], 'url': f['url'],
                'jobs_fetched': len(frames[f['feed_name']]), 'rows_merged': 0,
                'max_feed_date': (statuses[f['feed_name']]['max_date'].isoformat()
                                  if statuses[f['feed_name']]['max_date'] else None),
                'status': statuses[f['feed_name']]['status'],
                'message': statuses[f['feed_name']]['message'] or None,
            } for f in feeds])
            print("  logged the aborted run to t00_feed_runs")
        sys.exit(1)

    if args.dry_run:
        total = sum(len(d) for d in frames.values())
        print(f"\n[DRY RUN] No writes. Parsed {total:,} rows across {len(feeds)} feeds.")
        if empty_feeds:
            print(f"[DRY RUN] Would warn on empty feed(s): {', '.join(empty_feeds)}")
        if map_problems:
            print(f"[DRY RUN] Would flag {len(map_problems)} feed-map problem(s) (exit 2)")
        print(f"Completed in {(datetime.now() - start).total_seconds():.0f}s")
        return

    # MERGE + run-log
    print(f"\n{'='*72}\nUpserting (MERGE) into Bronze tables\n{'='*72}")
    run_rows = []
    for feed in feeds:
        name, rows = feed['feed_name'], frames[feed['feed_name']]
        st = statuses[name]
        status, msg = st['status'], st['message']
        merged = 0
        if not rows:
            print(f"  {name:14s} 0 rows — skipped")
        else:
            try:
                cs = content_schemas[feed['shape']]
                content_cols = [fld.name for fld in cs]
                target_id = ensure_target(client, feed, cs)
                staging_id = load_staging(client, feed, rows, cs)
                merged = run_merge(client, feed, target_id, staging_id, content_cols, run_ts) or 0
                print(f"  {name:14s} MERGE OK ({merged:,} rows inserted/updated)")
            except Exception as e:
                status, msg = 'error', f"{type(e).__name__}: {e}"
                print(f"  {name:14s} FAILED — {msg}")
        run_rows.append({
            'run_ts': run_ts, 'feed_name': name, 'url': feed['url'],
            'jobs_fetched': len(rows), 'rows_merged': int(merged),
            'max_feed_date': st['max_date'].isoformat() if st['max_date'] else None,
            'status': status, 'message': msg or None,
        })

    write_run_log(client, run_rows)
    print(f"  logged {len(run_rows)} run rows to t00_feed_runs")
    print(f"\n{'='*72}\nCompleted in {(datetime.now() - start).total_seconds():.0f}s")

    # A feed whose MERGE threw is logged above and to t00_feed_runs, but the
    # other feeds still upserted. Exit 2 (completed-with-errors) so the
    # orchestrator can flag the whole run as failed while still rebuilding
    # downstream from the Bronze data that did update.
    errored = [r['feed_name'] for r in run_rows if r['status'] == 'error']
    if errored:
        print(f"WARNING: {len(errored)} feed(s) failed to MERGE: {', '.join(errored)}")
    for msg in map_problems:
        print(f"WARNING: feed map: {msg}")
    if errored or map_problems:
        sys.exit(2)


if __name__ == '__main__':
    main()
