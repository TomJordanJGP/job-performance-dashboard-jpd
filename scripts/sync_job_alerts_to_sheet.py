"""Sync the cleaned path-alert set into the 'Path' tab of the JPD job-alerts Sheet.

The Sheet mirrors JPD.t04_job_alerts_path and is the user-maintained source the
BigQuery refresh reads from. Idempotent / re-runnable:

  - first run (empty tab): write header + all rows
  - later runs: append rows whose alert_id is new, and update activated /
    notification_interval on rows whose alert_id already exists (so an
    activation or frequency change is reflected, never duplicated)

All writes use gspread's RAW value-input (the default), so ISO dates
("2024-06-10") and hex alert_ids stay as literal text — USER_ENTERED would
coerce dates to serials and numeric-looking ids to numbers.

Auth: service_account.json with the Sheets scope; the SA must be Editor on the Sheet.
Run after scripts/clean_job_alerts_path.py has produced the CSV.
"""
import argparse

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = "17KIYg5jlb6Pu__Y6yjPooInu6T6Ob27udm-dEE0ftU4"
TAB = "Path"
KEY = "alert_id"
MUTABLE = ["activated", "notification_interval"]  # updatable on an existing alert


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="job_alerts_path_clean.csv")
    ap.add_argument("--sheet-id", default=SPREADSHEET_ID)
    ap.add_argument("--tab", default=TAB)
    ap.add_argument("--creds", default="service_account.json")
    args = ap.parse_args()

    df = pd.read_csv(args.csv, dtype=str).fillna("")
    cols = list(df.columns)
    if cols[0] != KEY:
        raise SystemExit(f"expected first column '{KEY}', got '{cols[0]}'")

    gc = gspread.authorize(Credentials.from_service_account_file(args.creds, scopes=SCOPES))
    ws = gc.open_by_key(args.sheet_id).worksheet(args.tab)
    existing = ws.get_all_values()
    has_data = len(existing) >= 1 and any(existing[0])

    if not has_data:
        values = [cols] + df.values.tolist()
        ws.clear()
        ws.resize(rows=len(values), cols=len(cols))
        ws.update(values, "A1")  # RAW by default
        print(f"FULL WRITE: header + {len(df)} rows -> {args.tab!r}")
        return

    # Incremental sync
    header = existing[0]
    if header != cols:
        raise SystemExit(f"sheet header mismatch:\n  sheet={header}\n  csv  ={cols}")
    pos = {c: i for i, c in enumerate(cols)}
    # alert_id -> (sheet_row_number, existing_row_values)
    idx = {r[0]: (i, r) for i, r in enumerate(existing[1:], start=2) if r and r[0]}

    new_rows, updates = [], []
    for _, r in df.iterrows():
        aid = r[KEY]
        if aid not in idx:
            new_rows.append([r[c] for c in cols])
            continue
        srow, cur = idx[aid]
        for m in MUTABLE:
            curv = cur[pos[m]] if pos[m] < len(cur) else ""
            if r[m] != curv:
                cell = gspread.utils.rowcol_to_a1(srow, pos[m] + 1)
                updates.append({"range": cell, "values": [[r[m]]]})

    if new_rows:
        ws.append_rows(new_rows)  # RAW by default
    if updates:
        ws.batch_update(updates)  # RAW by default
    print(f"INCREMENTAL: appended {len(new_rows)} new alerts, updated {len(updates)} cells")


if __name__ == "__main__":
    main()
