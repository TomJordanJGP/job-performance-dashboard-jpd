#!/usr/bin/env python3
"""Build JPD.t02_job_table — Method A (registry-templated source_union).

02_build_job_table.sql is a TEMPLATE. The per-feed `source_union` SELECTs and
the source-priority CASE are NOT hand-written — they're generated here from the
source feeds in t00_feed_registry (feed_kind='source', ordered by priority) and
substituted into the {{SOURCE_UNION}} and {{SOURCE_PRIORITY_CASE}} placeholders.

So adding a matched feed = one INSERT into t00_feed_registry; the next build
picks it up automatically with no SQL edit. The Appcast overlay and the
self-service segment stay hand-written in the template — they're singular
special cases, not things you add more of.

Only org_id / organization_name / organization_type vary between source feeds:
  - organization_id uses the registry's `org_id_column` (e.g. Civil Service maps
    jobiqo_org_id -> organization_id)
  - shape='civil_service' has no organization_name/organization_type columns, so
    they're emitted as CAST(NULL AS STRING)
Everything else is selected verbatim from each t01_feed_* table.

Usage:
    venv/bin/python scripts/build_job_table.py            # build t02
    venv/bin/python scripts/build_job_table.py --print    # print generated SQL, don't run
"""

import os
import sys
import argparse

from google.cloud import bigquery
from google.oauth2.service_account import Credentials

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"
BQ_DATASET = "JPD"
SQL_TEMPLATE = os.path.join(script_dir, "02_build_job_table.sql")


def get_client():
    creds = Credentials.from_service_account_file(
        os.path.join(project_dir, "service_account.json"),
        scopes=["https://www.googleapis.com/auth/bigquery"])
    return bigquery.Client(credentials=creds, project=BQ_PROJECT, location="EU")


def _sql_str(s):
    """Escape a Python string for use as a BigQuery single-quoted literal."""
    return s.replace("'", "''")


def source_feeds(client):
    q = f"""
    SELECT feed_name, table_name, shape, org_id_column, priority
    FROM `{BQ_PROJECT}.{BQ_DATASET}.t00_feed_registry`
    WHERE active AND feed_kind = 'source'
    ORDER BY priority
    """
    feeds = [dict(r.items()) for r in client.query(q).result()]
    if not feeds:
        sys.exit("No active source feeds in t00_feed_registry — cannot build source_union.")
    for f in feeds:
        if f["priority"] is None:
            sys.exit(f"Source feed '{f['feed_name']}' has NULL priority — needed for dedupe ordering.")
    return feeds


def feed_select(feed):
    """One source_union SELECT block for a feed, matching the original SQL shape."""
    org_id = feed["org_id_column"] or "organization_id"
    if feed["shape"] == "civil_service":
        org_name = "CAST(NULL AS STRING)"
        org_type = "CAST(NULL AS STRING)"
    else:
        org_name = "organization_name"
        org_type = "organization_type"
    return f"""  SELECT
    '{_sql_str(feed['feed_name'])}' AS source_feed,
    external_id, title,
    {org_id} AS organization_id,
    {org_name} AS organization_name,
    {org_type} AS organization_type,
    occupation, category, working_pattern,
    salary_min, salary_max, salary_exact,
    salary_free_text, salary_type, salary_currency,
    start_date, close_date, last_seen,
    jgp_external_vacancy_id
  FROM `{BQ_PROJECT}.{BQ_DATASET}.{feed['table_name']}`"""


def build_sql(client):
    feeds = source_feeds(client)
    union = "\n  UNION ALL\n".join(feed_select(f) for f in feeds)
    case = "\n".join(
        f"          WHEN '{_sql_str(f['feed_name'])}' THEN {f['priority']}" for f in feeds)

    with open(SQL_TEMPLATE) as fh:
        tmpl = fh.read()
    for ph in ("{{SOURCE_UNION}}", "{{SOURCE_PRIORITY_CASE}}"):
        count = tmpl.count(ph)
        if count != 1:
            sys.exit(f"Template placeholder {ph} must appear exactly once in "
                     f"02_build_job_table.sql (found {count}) — a stray copy in a "
                     f"comment would get the generated SQL injected into it.")
    return (tmpl.replace("{{SOURCE_UNION}}", union)
                .replace("{{SOURCE_PRIORITY_CASE}}", case), [f["feed_name"] for f in feeds])


def main():
    ap = argparse.ArgumentParser(description="Build t02_job_table from the registry-templated SQL")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="Print the generated SQL and exit (don't run)")
    args = ap.parse_args()

    client = get_client()
    sql, feed_names = build_sql(client)

    if args.print_only:
        print(sql)
        return

    print(f"Building t02_job_table — source feeds: {', '.join(feed_names)}")
    client.query(sql).result()

    table = f"{BQ_PROJECT}.{BQ_DATASET}.t02_job_table"
    n = list(client.query(f"SELECT COUNT(*) AS n FROM `{table}`").result())[0].n
    print(f"  t02_job_table rebuilt: {n:,} rows")
    for r in client.query(
            f"SELECT source_feed, COUNT(*) AS n FROM `{table}` GROUP BY source_feed ORDER BY n DESC").result():
        print(f"    {r.source_feed:14s} {r.n:,}")


if __name__ == "__main__":
    main()
