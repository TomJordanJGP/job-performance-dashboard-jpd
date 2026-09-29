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
Then a freshness check (see FRESHNESS_CHECKS): a source that has stopped updating
fails the run even when every step succeeded.

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
from datetime import date, datetime, timezone

from google.cloud import bigquery
from google.oauth2.service_account import Credentials

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"
JPD = f"{BQ_PROJECT}.JPD"

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

# Freshness guard. A run can finish green while a source has quietly stopped
# updating: the upstream GA4 tables froze on 2026-08-26 and nothing flagged it for
# a month. After the rebuild, each source's newest data is compared with how far
# behind it normally runs; a source past its limit fails the run, so GitHub emails
# the workflow owner. Entries: (label, SQL returning DATE `newest`, max age in days).
FRESHNESS_CHECKS = [
    # GA4 lands intraday, so its newest day is normally today or yesterday.
    ("GA4 events",
     f"SELECT MAX(event_date_dt) AS newest FROM `{JPD}.t04_vacancy_events`", 2),
    # Google's GSC export runs 2-3 days behind. Checked per site so one site's
    # export stopping can't hide behind the other's.
    ("GSC Jobs Go Public",
     f"SELECT MAX(IF(impressions_jgp > 0, event_date, NULL)) AS newest FROM `{JPD}.t04_gsc_daily`", 5),
    ("GSC LG Jobs",
     f"SELECT MAX(IF(impressions_lg > 0, event_date, NULL)) AS newest FROM `{JPD}.t04_gsc_daily`", 5),
]

# The GA4 stall is known and with its owner, so until this date it only warns: a
# known problem shouldn't turn every run red and bury new failures. From this date
# a still-stale GA4 source fails the run like any other.
WARN_ONLY_UNTIL = {"GA4 events": date(2026, 10, 13)}


def check_freshness(client):
    """Print each source's freshness and return the labels that fail the run."""
    today = datetime.now(timezone.utc).date()
    results = []  # (label, detail, is_fresh)

    # Every feed in the latest ingest must be 'ok'. The Bronze step only logs
    # 'empty' and 'stale' feeds, so without this they'd pass silently.
    feeds = list(client.query(
        f"SELECT feed_name, status FROM `{JPD}.t00_feed_runs` "
        f"WHERE run_ts = (SELECT MAX(run_ts) FROM `{JPD}.t00_feed_runs`)").result())
    bad = [f"{r.feed_name}={r.status}" for r in feeds if r.status != "ok"]
    detail = f"{len(feeds) - len(bad)}/{len(feeds)} ok" + (f" ({', '.join(bad)})" if bad else "")
    results.append(("Feeds (latest ingest)", detail, bool(feeds) and not bad))

    for label, sql, max_age in FRESHNESS_CHECKS:
        newest = list(client.query(sql).result())[0].newest
        if newest is None:
            results.append((label, "no data", False))
        else:
            age = (today - newest).days
            results.append((label, f"newest {newest}, {age}d old (limit {max_age}d)", age <= max_age))

    failing = []
    for label, detail, fresh in results:
        if fresh:
            print(f"  {label:22s} {detail}")
            continue
        warn_until = WARN_ONLY_UNTIL.get(label)
        warn_only = warn_until is not None and today < warn_until
        print(f"  {label:22s} {detail}  <-- STALE"
              + (f" (warning only until {warn_until})" if warn_only else ""))
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Annotation, so it shows on the run's summary page, not just in the log.
            print(f"::{'warning' if warn_only else 'error'}::{label} is stale: {detail}")
        if not warn_only:
            failing.append(label)
    return failing


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

    print("\nFreshness check")
    stale = check_freshness(client)

    print("\n" + "=" * 64)
    print(f"{mode} complete in {time.time() - overall:.0f}s")
    if soft_failures:
        print(f"FLAGGED: {len(soft_failures)} step(s) completed with errors: "
              f"{', '.join(soft_failures)}")
    if stale:
        print(f"STALE: {len(stale)} source(s) past their freshness limit: {', '.join(stale)}")
    if soft_failures or stale:
        if args.dry_run:
            print("A live run would be marked failed.")
            return
        print("Downstream tables were still rebuilt; exiting non-zero so the run is marked failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
