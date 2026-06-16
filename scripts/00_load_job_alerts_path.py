"""Load the JPD job-alerts 'Path' sheet into JPD.t04_job_alerts_path (upsert).

Reads the user-maintained 'Path' tab of the JPD_Job_Alerts Google Sheet (the
clean, deduped, one-row-per-alert mirror) and MERGEs it into the native table on
alert_id:
  - new alert_id      -> INSERT
  - existing alert_id -> UPDATE activated + notification_interval only

Append-only: alerts removed from the Sheet are retained in BigQuery (demand
history). Cleaning / exploding / dedup happen upstream (clean_job_alerts_path.py
then sync_job_alerts_to_sheet.py); this step just lands the Sheet in BigQuery.

All columns land as STRING — created_date is ISO 'yyyy-mm-dd' text that mirrors
the Sheet exactly and SAFE_CASTs to DATE in the analysis views.

Auth: service_account.json — Sheets scope to read the tab, BigQuery scope to load.
Re-runnable. Intended to run inside scripts/daily_refresh.py.
"""
import argparse
import re

import gspread
import pandas as pd
from google.cloud import bigquery
from google.oauth2.service_account import Credentials

PROJECT = "site-monitoring-421401"
DATASET = "JPD"
TARGET = f"{PROJECT}.{DATASET}.t04_job_alerts_path"
STAGE = f"{PROJECT}.{DATASET}.t04_job_alerts_path_stage"
SPREADSHEET_ID = "17KIYg5jlb6Pu__Y6yjPooInu6T6Ob27udm-dEE0ftU4"
TAB = "Path"

COLUMNS = ["alert_id", "alert_type", "created_date", "created_time", "activated",
           "notification_interval", "location", "remote_options", "employment_type",
           "occupational_fields", "industries", "organisations", "source_path"]
SCHEMA = [bigquery.SchemaField(c, "STRING", mode=("REQUIRED" if c == "alert_id" else "NULLABLE"))
          for c in COLUMNS]

MERGE_SQL = f"""
MERGE `{TARGET}` T
USING `{STAGE}` S
ON T.alert_id = S.alert_id
WHEN MATCHED THEN UPDATE SET
  activated = S.activated,
  notification_interval = S.notification_interval
WHEN NOT MATCHED THEN INSERT ROW
"""

DESCRIPTION = (
    "Saved filter-driven ('Path') job-search alerts — one row per unique alert. "
    "MERGE-synced (upsert on alert_id; append-only) from the 'Path' tab of the "
    "JPD_Job_Alerts Google Sheet by scripts/00_load_job_alerts_path.py. "
    "occupational_fields / industries / organisations are ' | '-delimited multi-value."
)


def read_sheet(creds_path):
    creds = Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    ws = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet(TAB)
    vals = ws.get_all_values()
    return pd.DataFrame(vals[1:], columns=vals[0])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--creds", default="service_account.json")
    args = ap.parse_args()

    df = read_sheet(args.creds)
    if list(df.columns) != COLUMNS:
        raise SystemExit(f"sheet columns {list(df.columns)} != expected {COLUMNS}")

    # Canary: created_date must stay ISO yyyy-mm-dd (lessons.md date trap)
    iso = df["created_date"].str.match(r"^\d{4}-\d{2}-\d{2}$")
    n_baddate = int((~iso).sum())
    # Blank cells -> NULL for clean BigQuery semantics
    df = df.replace("", None)

    creds = Credentials.from_service_account_file(
        args.creds, scopes=["https://www.googleapis.com/auth/bigquery"])
    client = bigquery.Client(credentials=creds, project=PROJECT, location="EU")

    tbl = bigquery.Table(TARGET, schema=SCHEMA)
    tbl.clustering_fields = ["created_date"]
    tbl.description = DESCRIPTION
    client.create_table(tbl, exists_ok=True)

    before = client.get_table(TARGET).num_rows
    # STAGE is a transient buffer the MERGE reads from — load it, merge, then
    # always drop it so only the final table persists (finally = cleaned up even
    # if the MERGE fails).
    try:
        client.load_table_from_dataframe(
            df, STAGE,
            job_config=bigquery.LoadJobConfig(schema=SCHEMA, write_disposition="WRITE_TRUNCATE"),
        ).result()
        merge = client.query(MERGE_SQL)
        merge.result()
    finally:
        client.delete_table(STAGE, not_found_ok=True)
    after = client.get_table(TARGET).num_rows

    print(f"sheet rows read     : {len(df)}")
    print(f"non-ISO dates (warn): {n_baddate}")
    print(f"target rows before  : {before}")
    print(f"target rows after   : {after}")
    print(f"MERGE affected rows : {merge.num_dml_affected_rows}")


if __name__ == "__main__":
    main()
