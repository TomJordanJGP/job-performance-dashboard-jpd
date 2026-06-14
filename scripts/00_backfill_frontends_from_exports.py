#!/usr/bin/env python3
"""One-off backfill: tag Bronze `frontends` from Jobiqo per-frontend exports.

Context (2026-06-14)
--------------------
The JGP/LG split on the dashboard's "Live Vacancies Over Time" chart is driven by
the feed `frontends` field (comma-separated `jobsgopublic` / `lgjobs`), read from
the source-feed Bronze tables in scripts/06_create_summary_tables.sql. The feeds
only began emitting `frontends` relatively recently, so ~all historic (expired)
vacancies carried NULL frontends — under-splitting the per-board history.

Jobiqo can export the full job list per front-end. This one-off loads those two
exports and backfills the NULL gaps in Bronze:
  - jobsgopublic export -> add 'jobsgopublic'
  - lgjobs export       -> add 'lgjobs'

Why Bronze (not a t04 ref / t02 column): Bronze is an upsert model — a row that
has left the live feed is frozen (the daily MERGE only re-touches rows still in
the feed), so backfilled tags on historic rows persist indefinitely. Live rows
keep being maintained by the feed going forward (~98% already tagged). t06
already reads `frontends` from these source tables, so no downstream change is
needed for the tags to flow to the chart.

Join key: the source tables key on the bare `external_id` hash. The JGP export
prefixes it with a front-end id (`1_<hash>`), so we strip a leading `<digits>_`.
The LG export is already bare (the strip is a no-op there).

Additive + idempotent: only adds a board token where missing
(`frontends NOT LIKE '%token%'`) — never removes or reorders an existing tag, so
it's safe to re-run and never clobbers a feed-provided value.

NOT part of the daily pipeline (one-off, like the other 00_* loaders;
daily_refresh.py refuses to run 00_* scripts). New vacancies posted after the
export are covered by the live feed's `frontends`.

Inputs used on 2026-06-14:
  --jgp "jobs-export-1781431766 - JGP.csv"   (48,800 rows)
  --lg  "jobs-export-1781427391 - LH.csv"    (33,002 rows)
Result: 35,523 'jobsgopublic' + 24,997 'lgjobs' = 60,520 tags; live vacancies on
neither board fell 94 -> 6. (CSVs are not committed — feed/job data stays out of
the repo, same as the feed URLs.)

Usage:
    venv/bin/python scripts/00_backfill_frontends_from_exports.py \
        --jgp "jobs-export-1781431766 - JGP.csv" \
        --lg  "jobs-export-1781427391 - LH.csv"
"""
import argparse
import csv
import os
import sys

from google.cloud import bigquery
from google.oauth2.service_account import Credentials

PROJECT, DATASET = "site-monitoring-421401", "JPD"

# Source-feed Bronze tables that carry the `frontends` column (Appcast does not).
SOURCE_FEED_TABLES = [
    "t01_feed_ats", "t01_feed_scrape", "t01_feed_civil_service",
    "t01_feed_backfill", "t01_feed_jgp_london_backfill",
]

# Jobiqo export column index for the external-id hash ("External ID").
EXTID_COL = 1


def get_client():
    sa = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "service_account.json")
    if not os.path.exists(sa):
        sys.exit(f"service_account.json not found at {sa}")
    creds = Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/bigquery"])
    return bigquery.Client(credentials=creds, project=PROJECT, location="EU")


def load_staging(client, path, stg):
    """Load just the external_id column from a Jobiqo export into a staging table."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        next(r)  # header
        for row in r:
            ext = row[EXTID_COL].strip() if len(row) > EXTID_COL else ""
            if ext:
                rows.append({"external_id": ext})
    tbl = f"{PROJECT}.{DATASET}.{stg}"
    client.load_table_from_json(rows, tbl, job_config=bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        schema=[bigquery.SchemaField("external_id", "STRING")])).result()
    print(f"  loaded {len(rows):,} external_ids -> {stg}")
    return tbl


def backfill(client, stg, token):
    """Additively add `token` to frontends on every source-feed row whose bare
    external_id is in the staging export and that isn't already tagged."""
    total = 0
    for t in SOURCE_FEED_TABLES:
        sql = f"""
        UPDATE `{PROJECT}.{DATASET}.{t}` x
        SET frontends = IF(x.frontends IS NULL, '{token}', CONCAT(x.frontends, ',{token}'))
        WHERE x.external_id IN (
          SELECT DISTINCT REGEXP_REPLACE(external_id, r'^[0-9]+_', '')
          FROM `{PROJECT}.{DATASET}.{stg}` WHERE external_id IS NOT NULL
        )
        AND (x.frontends IS NULL OR x.frontends NOT LIKE '%{token}%')
        """
        job = client.query(sql)
        job.result()
        n = job.num_dml_affected_rows or 0
        total += n
        print(f"    {t:32s} {n:6,}")
    return total


def main():
    ap = argparse.ArgumentParser(description="Backfill Bronze frontends from Jobiqo front-end exports")
    ap.add_argument("--jgp", required=True, help="jobsgopublic export CSV path")
    ap.add_argument("--lg", required=True, help="lgjobs export CSV path")
    args = ap.parse_args()

    client = get_client()
    print("Loading exports to staging...")
    load_staging(client, args.jgp, "_stg_jgp_export")
    load_staging(client, args.lg, "_stg_lg_export")

    print("\nBackfilling frontends (additive, idempotent)...")
    print("  adding 'jobsgopublic':")
    n_jgp = backfill(client, "_stg_jgp_export", "jobsgopublic")
    print("  adding 'lgjobs':")
    n_lg = backfill(client, "_stg_lg_export", "lgjobs")

    print(f"\nDone. jobsgopublic +{n_jgp:,}, lgjobs +{n_lg:,}  (total {n_jgp + n_lg:,} tags)")


if __name__ == "__main__":
    main()
