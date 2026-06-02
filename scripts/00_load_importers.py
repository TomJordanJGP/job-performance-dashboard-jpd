#!/usr/bin/env python3
"""Load importer_mapping.csv into JPD.t04_importers (importer_id -> importer_name).

Small reference table giving the t06_summary_* tables a human importer name to
join onto the GA4 `importer_ID` that t05 carries through from t04_vacancy_events.
The dashboard's "Importer" dimension reads importer_name.

Re-run whenever importer_mapping.csv changes. Idempotent (WRITE_TRUNCATE).

Usage:
    venv/bin/python scripts/00_load_importers.py
"""

import os
import sys
import csv

from google.cloud import bigquery
from google.oauth2.service_account import Credentials

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)

BQ_PROJECT = "site-monitoring-421401"
BQ_DATASET = "JPD"
CSV_PATH = os.path.join(project_dir, "importer_mapping.csv")
TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.t04_importers"


def get_client():
    creds = Credentials.from_service_account_file(
        os.path.join(project_dir, "service_account.json"),
        scopes=["https://www.googleapis.com/auth/bigquery"])
    return bigquery.Client(credentials=creds, project=BQ_PROJECT, location="EU")


def main():
    if not os.path.exists(CSV_PATH):
        sys.exit(f"importer_mapping.csv not found at {CSV_PATH}")

    rows = []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            raw_id = (r.get("importer_id") or "").strip()
            if raw_id == "":
                continue
            rows.append({
                "importer_id": int(raw_id),                       # matches t05.importer_ID (INT64)
                "importer_name": (r.get("importer_name") or "").strip() or None,
            })
    if not rows:
        sys.exit("importer_mapping.csv has no usable rows.")

    client = get_client()
    schema = [
        bigquery.SchemaField("importer_id", "INT64"),
        bigquery.SchemaField("importer_name", "STRING"),
    ]
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", schema=schema)
    client.load_table_from_json(rows, TABLE, job_config=job_config).result()

    print(f"Loaded {len(rows)} importers into t04_importers:")
    for r in client.query(f"SELECT importer_id, importer_name FROM `{TABLE}` ORDER BY importer_id").result():
        print(f"  {r.importer_id:>3} -> {r.importer_name}")


if __name__ == "__main__":
    main()
