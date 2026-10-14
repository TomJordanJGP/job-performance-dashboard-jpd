#!/usr/bin/env python3
"""Build JPD.t02_job_table — Method A (registry-templated source_union).

02_build_job_table.sql is a TEMPLATE. The per-feed `source_union` SELECTs and
the source-priority CASE are NOT hand-written — they're generated here from the
source feeds in t00_feed_registry (feed_kind='source', ordered by priority) and
substituted into the {{SOURCE_UNION}} and {{SOURCE_PRIORITY_CASE}} placeholders.

So adding a feed = one row in t00_feed_registry, which 01_sync_bronze_feeds.py
now writes itself when a feed appears in the feed map; the next build picks it
up with no SQL edit. The Appcast overlay and the self-service segment stay
hand-written in the template — they're singular special cases, not things you
add more of.

Which feeds: every source feed that isn't `excluded` and has a Bronze table.
That includes RETIRED feeds (active = FALSE, dropped out of the feed map), so
their vacancies keep their salary/dates in t02. A retired feed's rows are frozen,
so in the dedupe it ranks below every live feed (priority + RETIRED_PRIORITY_OFFSET):
a vacancy that moved to a new feed takes the live row, not the stale one.

The same feed list also defines the view t01_source_feeds (source_feed,
external_id, entity_id, last_seen, frontends), which 06_create_summary_tables.sql
reads instead of naming each t01_feed_* table.

Only org_id / organization_name / organization_type vary between source feeds:
  - organization_id uses the registry's `org_id_column` (e.g. Civil Service maps
    jobiqo_org_id -> organization_id)
  - shape='civil_service' has no organization_name/organization_type columns, so
    they're emitted as CAST(NULL AS STRING)
Everything else is selected verbatim from each t01_feed_* table.

Usage:
    venv/bin/python scripts/build_job_table.py            # refresh the view, build t02
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
SOURCE_VIEW = f"{BQ_PROJECT}.{BQ_DATASET}.t01_source_feeds"

# Added to a retired feed's priority so it loses every dedupe tie to a live feed.
# Larger than any real priority (registry priorities count up from 1).
RETIRED_PRIORITY_OFFSET = 1000


def get_client():
    creds = Credentials.from_service_account_file(
        os.path.join(project_dir, "service_account.json"),
        scopes=["https://www.googleapis.com/auth/bigquery"])
    return bigquery.Client(credentials=creds, project=BQ_PROJECT, location="EU")


def _sql_str(s):
    """Escape a Python string for use as a BigQuery single-quoted literal."""
    return s.replace("'", "''")


def source_feeds(client):
    """Non-excluded source feeds (live or retired) that have a Bronze table. A feed
    registered but not yet ingested has no table, so it's skipped until it does."""
    q = f"""
    SELECT r.feed_name, r.table_name, r.shape, r.org_id_column, r.priority, r.active,
           t.table_name IS NOT NULL AS has_table
    FROM `{BQ_PROJECT}.{BQ_DATASET}.t00_feed_registry` r
    LEFT JOIN `{BQ_PROJECT}.{BQ_DATASET}.INFORMATION_SCHEMA.TABLES` t
      ON t.table_name = r.table_name
    WHERE r.feed_kind = 'source' AND NOT COALESCE(r.excluded, FALSE)
    ORDER BY r.priority
    """
    rows = [dict(r.items()) for r in client.query(q).result()]
    for f in rows:
        if not f["has_table"]:
            print(f"  note: '{f['feed_name']}' has no Bronze table yet — left out of this build")
    feeds = [f for f in rows if f["has_table"]]
    if not any(f["active"] for f in feeds):
        sys.exit("No active source feeds with a Bronze table — cannot build source_union.")
    for f in feeds:
        if f["priority"] is None:
            sys.exit(f"Source feed '{f['feed_name']}' has NULL priority — needed for dedupe ordering.")
    return feeds


def dedupe_rank(feed):
    return feed["priority"] + (0 if feed["active"] else RETIRED_PRIORITY_OFFSET)


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


def view_sql(feeds):
    """t01_source_feeds: the per-feed columns 06 needs, stacked over the same feeds
    as source_union, so a new feed reaches t06 without a SQL edit."""
    union = "\nUNION ALL\n".join(
        f"SELECT '{_sql_str(f['feed_name'])}' AS source_feed, "
        f"external_id, entity_id, last_seen, frontends\n"
        f"FROM `{BQ_PROJECT}.{BQ_DATASET}.{f['table_name']}`" for f in feeds)
    return f"CREATE OR REPLACE VIEW `{SOURCE_VIEW}` AS\n{union}"


def build_sql(feeds):
    union = "\n  UNION ALL\n".join(feed_select(f) for f in feeds)
    case = "\n".join(
        f"          WHEN '{_sql_str(f['feed_name'])}' THEN {dedupe_rank(f)}" for f in feeds)

    with open(SQL_TEMPLATE) as fh:
        tmpl = fh.read()
    for ph in ("{{SOURCE_UNION}}", "{{SOURCE_PRIORITY_CASE}}"):
        count = tmpl.count(ph)
        if count != 1:
            sys.exit(f"Template placeholder {ph} must appear exactly once in "
                     f"02_build_job_table.sql (found {count}) — a stray copy in a "
                     f"comment would get the generated SQL injected into it.")
    return (tmpl.replace("{{SOURCE_UNION}}", union)
                .replace("{{SOURCE_PRIORITY_CASE}}", case))


def main():
    ap = argparse.ArgumentParser(description="Build t02_job_table from the registry-templated SQL")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="Print the generated SQL and exit (don't run)")
    args = ap.parse_args()

    client = get_client()
    feeds = source_feeds(client)
    view, sql = view_sql(feeds), build_sql(feeds)

    if args.print_only:
        print(view + ";\n")
        print(sql)
        return

    labels = [f["feed_name"] + ("" if f["active"] else " (retired)") for f in feeds]
    print(f"Building t02_job_table — source feeds: {', '.join(labels)}")
    client.query(view).result()
    print(f"  view t01_source_feeds refreshed ({len(feeds)} feeds)")
    client.query(sql).result()

    table = f"{BQ_PROJECT}.{BQ_DATASET}.t02_job_table"
    n = list(client.query(f"SELECT COUNT(*) AS n FROM `{table}`").result())[0].n
    print(f"  t02_job_table rebuilt: {n:,} rows")
    for r in client.query(
            f"SELECT source_feed, COUNT(*) AS n FROM `{table}` GROUP BY source_feed ORDER BY n DESC").result():
        print(f"    {r.source_feed:14s} {r.n:,}")


if __name__ == "__main__":
    main()
