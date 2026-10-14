#!/usr/bin/env python3
"""Bronze ingest for the JGP ATS vacancy_export API -> JPD.t01_ats_api.

Current-state UPSERT: one row per jgp_external_vacancy_id holding the latest raw
JSON payload + etag + fetch metadata. The response is stored RAW (STRING) — Silver
(t04_ats_*) parses it. Two modes:

  backfill     Fetch EVERY vacancy that carries a jgp_external_vacancy_id (~23k)
               and WRITE_TRUNCATE the table. One-off / occasional full refresh.
  incremental  (default) Re-fetch live vacancies + those closed in the last
               90 days (hires are recorded weeks after closing), sending
               If-None-Match so unchanged vacancies return 304 (skipped); changed
               ones are MERGE-upserted. Runs at most once a day: if the last clean
               run in t00_ats_runs is under 20h old it exits straight away, so the
               4-hourly refresh only calls the API daily (--force overrides).

Credentials come from the environment (ATS_API_KEY, ATS_API_BASE_URL — GitHub
secrets in CI), else from .ATS_API_KEY.env at the repo root (gitignored via *.env).
The key is NEVER logged, and the API host is kept out of this public repo the same
way. Be a good citizen: request starts are paced to --rps.

Exit codes follow daily_refresh.py: 0 = ok or skipped by the daily gate; 2 = soft
failure (missing credentials, auth rejected, or any fetch error) so the rest of the
refresh still runs but the run is marked failed; 1 = unexpected crash.

Usage:
    venv/bin/python scripts/07_sync_ats_api.py --mode backfill            # full ~23k
    venv/bin/python scripts/07_sync_ats_api.py                            # incremental (daily-gated)
    venv/bin/python scripts/07_sync_ats_api.py --force                    # incremental, ignore the gate
    venv/bin/python scripts/07_sync_ats_api.py --mode backfill --dry-run  # plan only, no calls/writes
    venv/bin/python scripts/07_sync_ats_api.py --mode backfill --limit 50 # smoke test (upserts 50)
"""
import os
import sys
import time
import argparse
import threading
from datetime import datetime, timezone
from itertools import chain
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from google.cloud import bigquery
from google.oauth2.service_account import Credentials

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"
BQ_DATASET = "JPD"
TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.t01_ats_api"
STAGING = f"{BQ_PROJECT}.{BQ_DATASET}.t01_ats_api_staging"
RUNS = f"{BQ_PROJECT}.{BQ_DATASET}.t00_ats_runs"
SOURCE_IDS = f"{BQ_PROJECT}.{BQ_DATASET}.t02_job_table"
VACANCIES = f"{BQ_PROJECT}.{BQ_DATASET}.t06_summary_vacancy"
KEY_FILE = os.path.join(project_dir, ".ATS_API_KEY.env")
USER_AGENT = "JPD-dashboard-ats-sync/1.0 (Jobs Go Public reporting)"

SOFT_FAIL_EXIT = 2      # matches daily_refresh.py: "finished, but flagged errors"
RECENT_DAYS = 90        # incremental: also re-fetch vacancies closed this recently
MIN_HOURS_BETWEEN = 20  # incremental: daily gate (the refresh itself runs every 4h)


def get_client():
    sa = os.path.join(project_dir, "service_account.json")
    if not os.path.exists(sa):
        sys.exit(f"service_account.json not found at {sa}")
    creds = Credentials.from_service_account_file(
        sa, scopes=["https://www.googleapis.com/auth/bigquery"])
    return bigquery.Client(credentials=creds, project=BQ_PROJECT, location="EU")


def load_credentials():
    """(api_key, base_url) from the environment, else from .ATS_API_KEY.env
    (dotenv-style ATS_API_KEY=... / ATS_API_BASE_URL=...; a bare line is taken as the
    key, the original format). Neither value is ever printed."""
    key = os.environ.get("ATS_API_KEY", "").strip()
    base = os.environ.get("ATS_API_BASE_URL", "").strip()
    if (not key or not base) and os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            for line in f.read().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    key = key or line
                    continue
                k, v = (x.strip() for x in line.split("=", 1))
                v = v.strip('"').strip("'")
                if k.upper() == "ATS_API_BASE_URL":
                    base = base or v
                elif "ATS_API_KEY" in k.upper():
                    key = key or v
    missing = [n for n, v in (("ATS_API_KEY", key), ("ATS_API_BASE_URL", base)) if not v]
    if missing:
        print(f"ATS credentials missing: {', '.join(missing)} — set them as environment "
              f"variables (GitHub secrets in CI) or in {os.path.basename(KEY_FILE)}.")
        sys.exit(SOFT_FAIL_EXIT)
    return key, base.rstrip("/")


def table_schema():
    S = bigquery.SchemaField
    return [
        S("jgp_external_vacancy_id", "STRING", mode="REQUIRED"),  # the id we called with (PK)
        S("ats_vacancy_id", "INT64"),                             # response .id (verification)
        S("fetched_at", "TIMESTAMP", mode="REQUIRED"),
        S("http_status", "INT64"),
        S("etag", "STRING"),                                      # for If-None-Match next run
        S("n_applications", "INT64"),                             # convenience (drives incremental)
        S("n_media_sources", "INT64"),
        S("payload", "STRING"),                                   # raw JSON body; NULL on error
    ]


def ensure_table(client):
    try:
        client.get_table(TABLE)
    except Exception:
        t = bigquery.Table(TABLE, schema=table_schema())
        t.clustering_fields = ["jgp_external_vacancy_id"]
        client.create_table(t)
        print(f"  created {TABLE}")


def ensure_runs_table(client):
    S = bigquery.SchemaField
    try:
        client.get_table(RUNS)
    except Exception:
        client.create_table(bigquery.Table(RUNS, schema=[
            S("run_ts", "TIMESTAMP", mode="REQUIRED"), S("mode", "STRING"),
            S("n_ids", "INT64"), S("n_200", "INT64"), S("n_304", "INT64"),
            S("n_gone", "INT64"), S("n_errors", "INT64"), S("ok", "BOOL"),
        ]))
        print(f"  created {RUNS}")


def hours_since_last_clean_run(client):
    """Hours since the newest run with no fetch errors, or None if there's none."""
    r = list(client.query(f"""
      SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(run_ts), MINUTE) / 60 AS h
      FROM `{RUNS}` WHERE ok""").result())[0]
    return r["h"]


def log_run(client, row):
    cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    client.load_table_from_json([row], RUNS, job_config=cfg).result()


def get_ids(client, mode, limit):
    if mode == "backfill":
        q = f"""
        SELECT DISTINCT jgp_external_vacancy_id AS id, CAST(NULL AS STRING) AS etag
        FROM `{SOURCE_IDS}`
        WHERE jgp_external_vacancy_id IS NOT NULL AND jgp_external_vacancy_id != ''
        """
    else:  # incremental: live vacancies + those closed in the last RECENT_DAYS
        # end_date comes from t06 (the resolved close date, estimate included) as of the
        # previous refresh; t02.is_live also catches vacancies too new to be in t06 yet.
        q = f"""
        WITH candidates AS (
          SELECT DISTINCT j.jgp_external_vacancy_id AS id
          FROM `{SOURCE_IDS}` j
          LEFT JOIN `{VACANCIES}` v ON v.entity_id_str = CAST(j.entity_id AS STRING)
          WHERE j.jgp_external_vacancy_id IS NOT NULL AND j.jgp_external_vacancy_id != ''
            AND (j.is_live OR v.is_live
                 OR DATE(v.end_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL {RECENT_DAYS} DAY))
        )
        SELECT c.id, a.etag
        FROM candidates c
        LEFT JOIN `{TABLE}` a ON a.jgp_external_vacancy_id = c.id
        """
    rows = [(str(r["id"]), r["etag"]) for r in client.query(q).result()]
    if limit:
        rows = rows[:limit]
    return rows


class RateLimiter:
    """Pace request *starts* to <= rps across all worker threads."""
    def __init__(self, rps):
        self.interval = 1.0 / rps if rps and rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def acquire(self):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            wait = self.next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self.next_at = max(now, self.next_at) + self.interval


def _retry_after(r, attempt):
    """Seconds to wait before retrying. Retry-After may be seconds or an HTTP date;
    a date (or anything unparseable) falls back to exponential backoff."""
    try:
        wait = float(r.headers.get("Retry-After", ""))
    except ValueError:
        wait = 2 ** attempt
    return min(max(wait, 0), 15)


def fetch_one(session, limiter, base_url, run_ts, item):
    vid, etag = item
    url = f"{base_url}/vacancies/{quote(str(vid), safe='')}/vacancy_export"
    headers = {"If-None-Match": etag} if etag else {}
    for attempt in range(4):
        limiter.acquire()
        try:
            r = session.get(url, headers=headers, timeout=45)
        except requests.RequestException:
            time.sleep(min(2 ** attempt, 10))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(_retry_after(r, attempt))
            continue
        if r.status_code == 304:
            return {"id": vid, "status": 304}
        if r.status_code == 200:
            ats_id = n_apps = n_media = None
            try:
                d = r.json()
                ats_id = d.get("id")
                n_apps = len(d.get("vacancy_applications") or [])
                n_media = len(d.get("vacancy_media_sources") or [])
            except ValueError:
                pass  # keep raw text even if it won't parse
            return {"id": vid, "status": 200, "row": dict(
                jgp_external_vacancy_id=vid, ats_vacancy_id=ats_id, fetched_at=run_ts,
                http_status=200, etag=r.headers.get("ETag"),
                n_applications=n_apps, n_media_sources=n_media, payload=r.text)}
        return {"id": vid, "status": r.status_code}
    return {"id": vid, "status": "retries_exhausted"}


def load_backfill(client, rows):
    cfg = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", schema=table_schema())
    client.load_table_from_json(rows, TABLE, job_config=cfg).result()


def load_incremental(client, rows):
    if not rows:
        return 0
    cfg = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", schema=table_schema())
    client.load_table_from_json(rows, STAGING, job_config=cfg).result()
    cols = [f.name for f in table_schema()]
    set_clause = ",\n      ".join(f"T.{c} = S.{c}" for c in cols if c != "jgp_external_vacancy_id")
    sql = f"""
    MERGE `{TABLE}` T
    USING `{STAGING}` S
    ON T.jgp_external_vacancy_id = S.jgp_external_vacancy_id
    WHEN MATCHED THEN UPDATE SET
      {set_clause}
    WHEN NOT MATCHED THEN INSERT ({', '.join(cols)})
    VALUES ({', '.join('S.' + c for c in cols)})
    """
    job = client.query(sql)
    job.result()
    client.query(f"DROP TABLE `{STAGING}`").result()
    return job.num_dml_affected_rows or 0


def main():
    ap = argparse.ArgumentParser(description="Bronze ingest: JGP ATS vacancy_export -> JPD.t01_ats_api")
    ap.add_argument("--mode", choices=["backfill", "incremental"], default="incremental")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rps", type=float, default=25.0, help="max request starts/sec across workers (0 = unlimited)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap #vacancies (testing; a limited backfill upserts instead of truncating)")
    ap.add_argument("--dry-run", action="store_true", help="plan only — no API calls, no writes")
    ap.add_argument("--force", action="store_true", help="incremental: ignore the once-a-day gate")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    client = get_client()
    ensure_table(client)
    ensure_runs_table(client)
    if args.mode == "incremental" and not args.force and not args.dry_run:
        h = hours_since_last_clean_run(client)
        if h is not None and h < MIN_HOURS_BETWEEN:
            print(f"ATS sync skipped — last clean run {h:.1f}h ago (daily gate: "
                  f"{MIN_HOURS_BETWEEN}h; --force to override).")
            return
    ids = get_ids(client, args.mode, args.limit)
    print(f"ATS sync — mode={args.mode}  vacancies={len(ids):,}  workers={args.workers}  rps={args.rps}")
    if args.dry_run:
        print("DRY RUN — no API calls, no writes.")
        return
    if not ids:
        print("No vacancies to fetch.")
        return

    api_key, base_url = load_credentials()
    print(f"API credentials loaded (key {len(api_key)} chars)")
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Authorization": f"ApiKey {api_key}",
                            "User-Agent": USER_AGENT})
    limiter = RateLimiter(args.rps)
    run_ts = datetime.now(timezone.utc).isoformat()

    # Probe one vacancy first: a rejected key fails the run now instead of after
    # thousands of 401s.
    probe = fetch_one(session, limiter, base_url, run_ts, ids[0])
    if probe.get("status") in (401, 403):
        print(f"ATS API rejected the credentials (HTTP {probe['status']}) — stopping.")
        sys.exit(SOFT_FAIL_EXIT)

    rows, n304, n_gone, errors, done = [], 0, 0, [], 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch_one, session, limiter, base_url, run_ts, it) for it in ids[1:]]
        for res in chain([probe], (f.result() for f in as_completed(futs))):
            done += 1
            st = res.get("status")
            if st == 200 and "row" in res:
                rows.append(res["row"])
            elif st == 304:
                n304 += 1
            elif st == 404:
                n_gone += 1  # vacancy no longer in the ATS — not a pipeline failure
            else:
                errors.append((res.get("id"), st))
                # backfill records the failure (null payload); incremental must NOT
                # overwrite an existing good row with an error, so it just logs it.
                if args.mode == "backfill":
                    rows.append(dict(
                        jgp_external_vacancy_id=res.get("id"), ats_vacancy_id=None,
                        fetched_at=run_ts, http_status=(st if isinstance(st, int) else None),
                        etag=None, n_applications=None, n_media_sources=None, payload=None))
            if done % 2000 == 0:
                print(f"  {done:,}/{len(ids):,}  ({time.time()-t0:.0f}s)  "
                      f"200={sum(1 for r in rows if r.get('payload'))} 304={n304} err={len(errors)}")
    n200 = sum(1 for r in rows if r.get('payload'))
    print(f"fetched {done:,} in {time.time()-t0:.0f}s  |  "
          f"200(payload)={n200}  304={n304}  gone(404)={n_gone}  errors={len(errors)}")

    if args.mode == "backfill" and not args.limit:
        load_backfill(client, rows)
        print(f"  WRITE_TRUNCATE {len(rows):,} rows -> {TABLE}")
    else:
        merged = load_incremental(client, [r for r in rows if r.get("payload") is not None])
        print(f"  MERGE upserted {merged:,} changed rows -> {TABLE}")

    log_run(client, dict(run_ts=run_ts, mode=args.mode, n_ids=len(ids), n_200=n200,
                         n_304=n304, n_gone=n_gone, n_errors=len(errors), ok=not errors))

    v = list(client.query(f"""
      SELECT COUNT(*) AS n_rows, COUNT(DISTINCT jgp_external_vacancy_id) AS n_ids,
             COUNTIF(payload IS NOT NULL) AS with_payload, COUNTIF(http_status = 200) AS http_ok,
             SUM(n_applications) AS total_apps, SUM(n_media_sources) AS total_media
      FROM `{TABLE}`""").result())[0]
    print(f"VERIFY {TABLE}: rows={v['n_rows']:,} distinct_ids={v['n_ids']:,} "
          f"with_payload={v['with_payload']:,} http200={v['http_ok']:,} "
          f"apps={v['total_apps'] or 0:,} media_rows={v['total_media'] or 0:,}")

    if errors:
        print(f"FLAGGED {len(errors)} fetch error(s). sample: "
              + ", ".join(f"{i}:{s}" for i, s in errors[:10]))
        sys.exit(SOFT_FAIL_EXIT)


if __name__ == "__main__":
    main()
