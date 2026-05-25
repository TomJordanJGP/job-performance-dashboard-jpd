#!/usr/bin/env python3
"""Daily refresh script for the Job Performance Dashboard.

Runs steps in sequence:
1.    Incremental sync: Append new events from the source table
2.    Sync feeds: Update job_metadata from XML feeds
2.05  Sync external ID additions: MERGE approved metadata from review Sheet into job_metadata
2.1.  Sync location additions: MERGE approved locations from review Sheet into lookup
2.2.  Refresh vacancy_locations: Rebuild exploded location table from job_metadata
2.5.  Enrich from HQ: Backfill HQ region/county on job_metadata
3.    Rebuild enriched table: Re-join with metadata, locations, region canonical + Tier 4 HQ
4.    Rebuild aggregated tables: Pre-compute vacancy summary and daily totals
5.    Refresh reconciliation: Rebuild missing_external_ids table
5.5   Export missing IDs: Overwrite Missing IDs Sheet tab with current outstanding vacancies
6.    Export unmatched towns: Overwrite Review Sheet tab with current unmatched towns

Can be run manually, via cron, or as a GitHub Action.

Usage:
    python scripts/daily_refresh.py
    python scripts/daily_refresh.py --dry-run    # Preview without executing
"""

import os
import sys
import subprocess
import argparse
from datetime import datetime

# Add parent directory to path so we can find service_account.json
script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

def get_client():
    """Initialize BigQuery client."""
    from google.oauth2.service_account import Credentials
    from google.cloud import bigquery

    sa_path = os.path.join(project_dir, 'service_account.json')
    if not os.path.exists(sa_path):
        print(f"ERROR: service_account.json not found at {sa_path}")
        sys.exit(1)

    creds = Credentials.from_service_account_file(sa_path, scopes=[
        'https://www.googleapis.com/auth/bigquery',
        'https://www.googleapis.com/auth/drive.readonly',
    ])
    return bigquery.Client(credentials=creds, project='site-monitoring-421401')


def run_sql_file(client, filename, description, dry_run=False):
    """Run a SQL file against BigQuery."""
    sql_path = os.path.join(script_dir, filename)
    if not os.path.exists(sql_path):
        print(f"  ERROR: {sql_path} not found")
        return False

    with open(sql_path) as f:
        sql = f.read()

    # Remove comment-only lines and split into statements
    lines = [line for line in sql.split('\n') if not line.strip().startswith('--')]
    clean_sql = '\n'.join(lines)
    statements = [s.strip() for s in clean_sql.split(';') if s.strip()]

    print(f"\n{'='*60}")
    print(f"Step: {description}")
    print(f"File: {filename}")
    print(f"Statements: {len(statements)}")

    if dry_run:
        print("  [DRY RUN] Would execute:")
        for i, stmt in enumerate(statements):
            preview = stmt[:100].replace('\n', ' ')
            print(f"    {i+1}. {preview}...")
        return True

    for i, stmt in enumerate(statements):
        try:
            print(f"  Running statement {i+1}/{len(statements)}...", end=' ', flush=True)
            job = client.query(stmt)
            job.result()
            if job.num_dml_affected_rows is not None:
                print(f"OK ({job.num_dml_affected_rows:,} rows affected)")
            else:
                print("OK")
        except Exception as e:
            print(f"FAILED: {e}")
            return False

    return True


def verify_tables(client):
    """Print current state of all tables."""
    print(f"\n{'='*60}")
    print("Verification:")

    tables = [
        ('job_performance_details_combined', 'MAX(event_date)'),
        ('job_metadata', 'MAX(last_updated)'),
        ('vacancy_locations', 'COUNT(DISTINCT entity_id)'),
        ('feed_jobs_latest', 'MAX(last_seen)'),
        ('region_canonical', 'COUNT(*)'),
        ('job_performance_enriched', 'MAX(event_date_parsed)'),
        ('dashboard_vacancy_summary', 'MAX(last_event_date)'),
        ('dashboard_daily_totals', 'MAX(event_date)'),
        ('weekly_live_vacancies', 'MAX(week_start)'),
        ('missing_external_ids', 'COUNT(*)'),
    ]

    for table, max_date_expr in tables:
        try:
            q = f"SELECT COUNT(*) as cnt, {max_date_expr} as max_d FROM `site-monitoring-421401.job_data_export.{table}`"
            r = client.query(q).to_dataframe()
            print(f"  {table}: {r.iloc[0]['cnt']:,} rows, latest: {r.iloc[0]['max_d']}")
        except Exception as e:
            print(f"  {table}: ERROR - {e}")


def main():
    parser = argparse.ArgumentParser(description='Daily refresh for Job Performance Dashboard')
    parser.add_argument('--dry-run', action='store_true', help='Preview without executing')
    args = parser.parse_args()

    start_time = datetime.now()
    print(f"Job Performance Dashboard - Daily Refresh")
    print(f"Started: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    client = get_client()

    # Step 1: Incremental sync
    ok = run_sql_file(client, 'incremental_sync_combined.sql',
                      'Sync new events from source table', args.dry_run)
    if not ok:
        print("FAILED at step 1. Aborting.")
        sys.exit(1)

    # Step 2: Sync feeds to update job_metadata
    print(f"\n{'='*60}")
    print("Step: Sync job feeds (update job_metadata)")
    sync_feeds_path = os.path.join(script_dir, 'sync_feeds.py')
    if args.dry_run:
        print("  [DRY RUN] Would run sync_feeds.py")
    else:
        result = subprocess.run(
            [sys.executable, sync_feeds_path],
            capture_output=True, text=True
        )
        # Always print full output for visibility (especially in GitHub Actions logs)
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")
        if result.returncode != 0:
            print(f"  WARNING: Feed sync failed (exit code {result.returncode}):")
            if result.stderr.strip():
                for line in result.stderr.strip().split('\n'):
                    print(f"  STDERR: {line}")
            print("  Continuing with existing feed data...")

    # Step 2.05: Sync approved external ID / metadata additions from Google Sheet → job_metadata.
    # Must run BEFORE enriched table rebuild so new metadata rows are available for enrichment.
    ok = run_sql_file(client, 'sync_external_id_additions.sql',
                      'Sync approved external ID additions from Sheet', args.dry_run)
    if not ok:
        print("  WARNING: External ID additions sync failed. Continuing with existing metadata...")

    # Step 2.06: Sync approved entity_id fills from Google Sheet → job_metadata.
    # Counterpart to 2.05 keyed the other direction — matches on external_id to
    # fill in entity_id + employer_type + original_publishing_date on feed rows
    # that don't yet have a Jobiqo entity_id.
    ok = run_sql_file(client, 'sync_entity_id_additions.sql',
                      'Sync approved entity_id additions from Sheet', args.dry_run)
    if not ok:
        print("  WARNING: Entity ID additions sync failed. Continuing with existing metadata...")

    # Post-sync data-quality probe: NULL counts on date columns for sheet-synced
    # rows. Sustained increases here flag silent date-parse regressions
    # (e.g. a new Sheet date format the SQL parser can't handle).
    if not args.dry_run:
        try:
            null_counts = client.query("""
                SELECT
                  COUNTIF(original_publishing_date IS NULL) AS orig_null,
                  COUNTIF(publishing_date IS NULL) AS pub_null,
                  COUNTIF(expiration_date IS NULL) AS exp_null,
                  COUNT(*) AS total_rows
                FROM `site-monitoring-421401.job_data_export.job_metadata`
                WHERE entity_id IS NOT NULL AND entity_id != ''
            """).result().to_dataframe().iloc[0]
            print(
                f"  post-sync NULLs (entity_id rows={null_counts['total_rows']}) — "
                f"orig:{null_counts['orig_null']} "
                f"pub:{null_counts['pub_null']} "
                f"exp:{null_counts['exp_null']}"
            )
        except Exception as e:
            print(f"  WARNING: post-sync NULL probe failed: {e}")

    # Step 2.1: Sync approved location additions from Google Sheet → location_lookup.
    # Must run BEFORE vacancy_locations refresh so new lookup entries are available.
    ok = run_sql_file(client, 'sync_location_additions.sql',
                      'Sync approved location additions from Sheet', args.dry_run)
    if not ok:
        print("  WARNING: Location additions sync failed. Continuing with existing lookup data...")

    # Step 2.2: Refresh vacancy_locations from job_metadata.locations
    ok = run_sql_file(client, 'refresh_vacancy_locations.sql',
                      'Rebuild vacancy_locations from job_metadata', args.dry_run)
    if not ok:
        print("  WARNING: vacancy_locations refresh failed. Continuing with existing data...")

    # Step 2.5: Enrich from HQ addresses
    ok = run_sql_file(client, 'enrich_from_hq.sql',
                      'Enrich job_metadata with HQ region/county', args.dry_run)
    if not ok:
        print("  WARNING: HQ enrichment failed. Continuing with existing data...")

    # Step 3: Rebuild enriched table
    ok = run_sql_file(client, 'refresh_enriched_table.sql',
                      'Rebuild enriched table with metadata + locations', args.dry_run)
    if not ok:
        print("FAILED at step 3. Aborting.")
        sys.exit(1)

    # Step 4: Rebuild aggregated tables
    ok = run_sql_file(client, 'create_aggregated_tables.sql',
                      'Rebuild dashboard summary tables', args.dry_run)
    if not ok:
        print("FAILED at step 4. Aborting.")
        sys.exit(1)

    # Step 5: Refresh reconciliation tables (missing_external_ids)
    ok = run_sql_file(client, 'create_reconciliation_tables.sql',
                      'Refresh reconciliation tables', args.dry_run)
    if not ok:
        print("  WARNING: Reconciliation refresh failed. Non-critical, continuing...")

    # Step 5.5: Detect and export missing external IDs to review Sheet.
    # Runs after reconciliation refresh so we have up-to-date missing_external_ids data.
    print(f"\n{'='*60}")
    print("Step: Detect and export missing external IDs to review Sheet")
    export_missing_ids_script = os.path.join(script_dir, 'export_missing_ids_to_sheet.py')
    if args.dry_run:
        print("  [DRY RUN] Would run export_missing_ids_to_sheet.py")
    elif os.path.exists(export_missing_ids_script):
        result = subprocess.run(
            [sys.executable, export_missing_ids_script],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")
        if result.returncode != 0:
            print(f"  WARNING: Missing ID export failed (exit code {result.returncode})")
            if result.stderr.strip():
                for line in result.stderr.strip().split('\n'):
                    print(f"  STDERR: {line}")
            print("  Non-critical — continuing...")
    else:
        print(f"  SKIPPED: {export_missing_ids_script} not found")

    # Step 5.6: Detect and export vacancies missing entity_id to review Sheet.
    # Counterpart to 5.5 — reads vacancies_missing_entity_id (maintained by
    # sync_feeds.py earlier in the run) and overwrites the "Missing Entity IDs"
    # tab.
    print(f"\n{'='*60}")
    print("Step: Detect and export missing entity IDs to review Sheet")
    export_missing_entity_ids_script = os.path.join(script_dir, 'export_missing_entity_ids_to_sheet.py')
    if args.dry_run:
        print("  [DRY RUN] Would run export_missing_entity_ids_to_sheet.py")
    elif os.path.exists(export_missing_entity_ids_script):
        result = subprocess.run(
            [sys.executable, export_missing_entity_ids_script],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")
        if result.returncode != 0:
            print(f"  WARNING: Missing entity ID export failed (exit code {result.returncode})")
            if result.stderr.strip():
                for line in result.stderr.strip().split('\n'):
                    print(f"  STDERR: {line}")
            print("  Non-critical — continuing...")
    else:
        print(f"  SKIPPED: {export_missing_entity_ids_script} not found")

    # Step 6: Detect and append new unmatched towns to review Sheet.
    # Runs after vacancy_locations refresh so we have up-to-date unmatched data.
    print(f"\n{'='*60}")
    print("Step: Detect and export unmatched towns to review Sheet")
    export_script = os.path.join(script_dir, 'export_unmatched_to_sheet.py')
    if args.dry_run:
        print("  [DRY RUN] Would run export_unmatched_to_sheet.py")
    elif os.path.exists(export_script):
        result = subprocess.run(
            [sys.executable, export_script],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                print(f"  {line}")
        if result.returncode != 0:
            print(f"  WARNING: Unmatched town export failed (exit code {result.returncode})")
            if result.stderr.strip():
                for line in result.stderr.strip().split('\n'):
                    print(f"  STDERR: {line}")
            print("  Non-critical — continuing...")
    else:
        print(f"  SKIPPED: {export_script} not found")

    # Verify
    if not args.dry_run:
        verify_tables(client)

    elapsed = (datetime.now() - start_time).total_seconds()
    print(f"\n{'='*60}")
    print(f"Completed in {elapsed:.0f}s")


if __name__ == '__main__':
    main()
