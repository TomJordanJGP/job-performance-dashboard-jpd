"""One-command refresh for the path-alert demand table.

Run this after downloading a fresh JGP job-alert export. It chains the three
steps in order, stopping on the first failure:

  1. clean_job_alerts_path.py     xlsx  -> job_alerts_path_clean.csv  (clean/explode/dedup)
  2. sync_job_alerts_to_sheet.py  CSV   -> 'Path' tab  (append new alerts / update activated+freq)
  3. 00_load_job_alerts_path.py   Sheet -> JPD.t04_job_alerts_path  (MERGE upsert)

Reference loaders are manual here by design (daily_refresh.py excludes 00_load_*),
and the alerts Sheet only changes when you re-export — so this runs on demand,
only when there's genuinely new data.

Run from the project root (where service_account.json and the .xlsx live):
    venv/bin/python scripts/refresh_job_alerts.py --xlsx job-alert-export-XXXXXXXXXX.xlsx
"""
import argparse
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run(label, script, *script_args):
    print(f"\n=== {label} ===", flush=True)
    t = time.time()
    r = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, script), *script_args])
    if r.returncode != 0:
        sys.exit(f"FAILED at '{label}' (exit {r.returncode}) — pipeline stopped.")
    print(f"  [{time.time() - t:.0f}s]", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xlsx", default="job-alert-export-1781539016.xlsx",
                    help="Path to the fresh JGP job-alert export")
    ap.add_argument("--csv", default="job_alerts_path_clean.csv",
                    help="Intermediate cleaned CSV path")
    args = ap.parse_args()

    overall = time.time()
    run("1/3 clean + explode", "clean_job_alerts_path.py", "--xlsx", args.xlsx, "--out", args.csv)
    run("2/3 sync to Google Sheet", "sync_job_alerts_to_sheet.py", "--csv", args.csv)
    run("3/3 load to BigQuery", "00_load_job_alerts_path.py")
    print(f"\nRefresh complete in {time.time() - overall:.0f}s", flush=True)


if __name__ == "__main__":
    main()
