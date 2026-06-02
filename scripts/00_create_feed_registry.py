#!/usr/bin/env python3
"""Create + seed the feed registry and run-log tables in JPD.

Two tables, both prefixed t00_ (pre-pipeline config/reference — sorts first in
the BQ console):

  - t00_feed_registry : the hand-maintained list of polled feeds. ONE row per
    feed. The pipeline reads this before every run, so ADDING A FEED IS A SINGLE
    INSERT here — no code change. Feed URLs live ONLY in this table (never in
    git), which is why this script seeds them from a gitignored bootstrap file
    rather than hardcoding them.

  - t00_feed_runs : append-only log. One row per (feed, run) written by
    01_sync_bronze_feeds.py — when each feed was polled, how many jobs it
    returned, how many rows merged, the feed's own max date, and a status.
    Lets you spot a feed that's gone stale or empty.

Idempotent: re-running creates nothing that already exists and re-seeds nothing
that's already populated.

Seeding: reads `feed_registry_seed.json` from the project root (gitignored —
.gitignore ignores all *.json). Shape (one object per feed):
    {
      "feed_name": "ATS", "url": "https://…", "table_name": "t01_feed_ats",
      "feed_kind": "source",          # 'source' | 'appcast'
      "shape": "ats",                 # 'scrape' | 'ats' | 'civil_service' | 'appcast'
      "org_id_column": "organization_id",
      "priority": 1,                  # source dedupe order; null for the overlay
      "active": true, "notes": null
    }
If the registry is already populated the seed file is ignored.

Usage:
    venv/bin/python scripts/00_create_feed_registry.py
"""

import os
import sys
import json
from datetime import datetime, timezone

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"
BQ_DATASET = "JPD"
SEED_FILE = os.path.join(project_dir, "feed_registry_seed.json")

REGISTRY_TABLE = "t00_feed_registry"
RUNS_TABLE = "t00_feed_runs"


def get_client():
    from google.oauth2.service_account import Credentials
    from google.cloud import bigquery

    sa_path = os.path.join(project_dir, "service_account.json")
    if not os.path.exists(sa_path):
        print(f"ERROR: service_account.json not found at {sa_path}")
        sys.exit(1)
    creds = Credentials.from_service_account_file(
        sa_path, scopes=["https://www.googleapis.com/auth/bigquery"])
    return bigquery.Client(credentials=creds, project=BQ_PROJECT, location="EU")


def registry_schema():
    from google.cloud import bigquery
    S = lambda n, t="STRING": bigquery.SchemaField(n, t)
    return [
        S("feed_name"), S("url"), S("table_name"),
        S("feed_kind"), S("shape"), S("org_id_column"),
        S("priority", "INT64"), S("active", "BOOL"),
        S("notes"), S("added_at", "TIMESTAMP"),
    ]


def runs_schema():
    from google.cloud import bigquery
    S = lambda n, t="STRING": bigquery.SchemaField(n, t)
    return [
        S("run_ts", "TIMESTAMP"), S("feed_name"), S("url"),
        S("jobs_fetched", "INT64"), S("rows_merged", "INT64"),
        S("max_feed_date", "TIMESTAMP"), S("status"), S("message"),
    ]


def ensure_table(client, name, schema):
    from google.cloud import bigquery
    table_id = f"{BQ_PROJECT}.{BQ_DATASET}.{name}"
    try:
        client.get_table(table_id)
        print(f"  {name}: exists")
    except Exception:
        client.create_table(bigquery.Table(table_id, schema=schema))
        print(f"  {name}: created")
    return table_id


def seed_registry(client, table_id):
    from google.cloud import bigquery

    n = list(client.query(f"SELECT COUNT(*) AS n FROM `{table_id}`").result())[0].n
    if n:
        print(f"  registry already has {n} rows — not seeding")
        return
    if not os.path.exists(SEED_FILE):
        print(f"  registry is empty and no seed file at {SEED_FILE}")
        print("  Create feed_registry_seed.json (see docstring) and re-run, "
              "or INSERT feeds by hand.")
        return

    with open(SEED_FILE) as f:
        rows = json.load(f)

    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("org_id_column", "organization_id")
        r.setdefault("active", True)
        r.setdefault("notes", None)
        r.setdefault("priority", None)
        r["added_at"] = now

    # Load job (not streaming insert) — writes straight to storage so the rows
    # are immediately editable (no streaming-buffer DML lock when you later
    # UPDATE a URL by hand).
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND", schema=registry_schema())
    client.load_table_from_json(rows, table_id, job_config=job_config).result()
    print(f"  seeded {len(rows)} feeds from feed_registry_seed.json")


def report(client, table_id):
    print("\nRegistry contents:")
    q = f"""
    SELECT feed_name, feed_kind, shape, priority, active,
           IF(url IS NULL OR url = '', '(no url)', 'set') AS url_state
    FROM `{table_id}`
    ORDER BY feed_kind, priority
    """
    for r in client.query(q).result():
        prio = r.priority if r.priority is not None else "-"
        print(f"  {r.feed_name:14s} kind={r.feed_kind:7s} shape={r.shape:13s} "
              f"prio={str(prio):>2s} active={str(r.active):5s} url={r.url_state}")


def main():
    client = get_client()
    print("Feed registry + run-log -> JPD")
    reg_id = ensure_table(client, REGISTRY_TABLE, registry_schema())
    ensure_table(client, RUNS_TABLE, runs_schema())
    seed_registry(client, reg_id)
    report(client, reg_id)


if __name__ == "__main__":
    main()
