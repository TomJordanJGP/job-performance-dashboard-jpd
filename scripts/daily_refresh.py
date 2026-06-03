#!/usr/bin/env python3
"""Daily refresh orchestrator for the JPD (Jobiqo) pipeline.

Runs the daily steps in dependency order with per-step timing. SQL steps run via
the BigQuery client (EU); Python steps run as subprocesses. Stops on the first
HARD failure — every downstream step depends on upstream output. A Python step
that exits 2 is a SOFT failure (it finished but flagged errors, e.g. one feed's
MERGE failed): downstream still rebuilds, but the whole run exits non-zero so CI
marks it failed.

Order:
    1. Bronze ingest        01_sync_bronze_feeds.py     -> t01_feed_*
    2. Silver: job table    build_job_table.py          -> t02_job_table
    3. Silver: GA4 sync     04_sync_ga4_events.sql      -> t04_vacancy_events
    4. Silver: GSC sync     04_sync_gsc_daily.sql       -> t04_gsc_daily
    5. Gold:   enriched     05_build_enriched_vacancies.sql -> t05_enriched_vacancies
    6. Gold:   summaries    06_create_summary_tables.sql    -> t06_summary_*

Does NOT run, by design:
    - One-off reference loaders 00_load_* (organisations, postcodes, importers,
      selfservice) — re-run manually only when their source data changes.
    - One-off backfills 04_create_and_backfill_* — the daily syncs (steps 3-4)
      keep those tables fresh.

Usage:
    venv/bin/python scripts/daily_refresh.py            # run the pipeline
    venv/bin/python scripts/daily_refresh.py --dry-run  # show plan + estimate SQL scan; no writes
"""

import os
import sys
import time
import argparse
import subprocess

from google.cloud import bigquery
from google.oauth2.service_account import Credentials

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"

# (label, kind, target). kind 'py' runs as a subprocess; 'sql' runs via the client.
STEPS = [
    ("Bronze ingest (t01_feed_*)",         "py",  "01_sync_bronze_feeds.py"),
    ("Silver: t02_job_table",              "py",  "build_job_table.py"),
    ("Silver: GA4 events sync (t04)",      "sql", "04_sync_ga4_events.sql"),
    ("Silver: GSC daily sync (t04)",       "sql", "04_sync_gsc_daily.sql"),
    ("Gold: t05_enriched_vacancies",       "sql", "05_build_enriched_vacancies.sql"),
    ("Gold: t06_summary_*",                "sql", "06_create_summary_tables.sql"),
]


def get_client():
    sa_path = os.path.join(project_dir, "service_account.json")
    if not os.path.exists(sa_path):
        sys.exit(f"service_account.json not found at {sa_path}")
    creds = Credentials.from_service_account_file(
        sa_path, scopes=["https://www.googleapis.com/auth/bigquery"])
    return bigquery.Client(credentials=creds, project=BQ_PROJECT, location="EU")


# Exit code a 'py' step uses to mean "finished, but flagged errors" (e.g. one
# feed failed to MERGE). Distinct from any other non-zero code, which is a hard
# failure that stops the pipeline.
SOFT_FAIL_EXIT = 2


def run_sql(client, filename, dry_run):
    sql = open(os.path.join(script_dir, filename)).read()
    if dry_run:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False))
        gb = job.total_bytes_processed / 1e9
        # Multi-statement scripts report 0 under dry-run; flag that rather than imply "free".
        note = f"would scan ~{gb:.2f} GB" if gb > 0 else "(multi-statement; scan size not estimable via dry-run)"
        return note, False
    client.query(sql).result()
    return "done", False


def run_py(filename, dry_run):
    path = os.path.join(script_dir, filename)
    if dry_run:
        return f"would run: {os.path.basename(sys.executable)} scripts/{filename}", False
    result = subprocess.run([sys.executable, path])
    if result.returncode == SOFT_FAIL_EXIT:
        return "completed WITH ERRORS (flagged — see step log above)", True
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args)
    return "done", False


def main():
    ap = argparse.ArgumentParser(description="JPD daily refresh orchestrator")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show the plan and estimate SQL scan bytes; no writes")
    args = ap.parse_args()

    # Guard: the daily pipeline must never run a one-off backfill / reference
    # loader (they CREATE OR REPLACE tables and/or scan full history). Fail loudly
    # if a future edit wires one into STEPS.
    _FORBIDDEN = ("create_and_backfill", "00_load_", "00_import_", "00_create_")
    bad = [t for _, _, t in STEPS if any(p in t for p in _FORBIDDEN)]
    if bad:
        sys.exit(f"refusing to run one-off/backfill script(s) in the daily pipeline: {', '.join(bad)}")

    # Line-buffer our stdout so step logs interleave in order with the inherited
    # output of the Python subprocess steps (Bronze ingest, t02 build).
    sys.stdout.reconfigure(line_buffering=True)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"JPD daily refresh — {mode}")
    print("=" * 64)
    client = get_client()
    overall = time.time()

    soft_failures = []
    for i, (label, kind, target) in enumerate(STEPS, 1):
        print(f"\n[{i}/{len(STEPS)}] {label}  ({target})")
        started = time.time()
        try:
            note, soft = run_sql(client, target, args.dry_run) if kind == "sql" \
                else run_py(target, args.dry_run)
        except subprocess.CalledProcessError as e:
            print(f"  FAILED (exit {e.returncode}) — stopping pipeline.")
            sys.exit(1)
        except Exception as e:
            print(f"  FAILED — {type(e).__name__}: {e}")
            print("  stopping pipeline.")
            sys.exit(1)
        if soft:
            soft_failures.append(label)
        print(f"  {note}  [{time.time() - started:.0f}s]")

    print("\n" + "=" * 64)
    print(f"{mode} complete in {time.time() - overall:.0f}s")
    if soft_failures:
        print(f"FLAGGED: {len(soft_failures)} step(s) completed with errors: "
              f"{', '.join(soft_failures)}")
        print("Downstream tables were still rebuilt; exiting non-zero so the run is marked failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
