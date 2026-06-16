"""Phase 1: clean + explode the JGP saved-search export into the path-alert set.

Reads the raw 'Job alerts' export and produces one clean row per unique
filter-driven ("Path") alert — i.e. alerts saved via the faceted UI rather than
a free-text search. Steps:

  1. Keep Path alerts only (empty Search column).
  2. Drop no-filter rows (none of the 6 filter columns populated) — the useless
     bare "/jobs" alerts that tell us nothing.
  3. Cell-split the multi-value filter columns into ' | '-delimited, trimmed,
     de-duplicated term lists:
       - Organisation name → split on a comma NOT followed by a space, so org
         names that contain an internal ', ' survive intact
         (e.g. "Ministry of Housing, Communities and Local Government").
       - Occupational field / Industry → split on every comma (controlled-vocab
         terms have no internal commas).
       - Remote options / Employment type / Location are NOT exploded.
  4. Assign a stable alert_id = SHA1 of the alert's identity columns, EXCLUDING
     the mutable Activated + Notification interval fields, so re-downloads of an
     unchanged alert dedupe and a later activation/frequency change updates the
     row rather than spawning a new one.
  5. Dedupe on alert_id (one row per alert).

Output is a CSV that mirrors JPD.t04_job_alerts_path and seeds the Google Sheet.
Deterministic and re-runnable: the same export always yields the same alert_ids.
"""
import argparse
import csv
import hashlib
import re

import pandas as pd

SHEET = "Job alerts"
FILTER_COLS = ["Location", "Remote options", "Employment type",
               "Occupational field", "Industry", "Organisation name"]
# Alert identity = every source column except the two mutable settings.
IDENTITY_EXCLUDE = {"Activated", "Notification interval"}
# Organisation delimiter: a comma NOT followed by a space (names carry ", ").
ORG_SPLIT = re.compile(r",(?=\S)")

OUTPUT_COLS = ["alert_id", "alert_type", "created_date", "created_time",
               "activated", "notification_interval", "location", "remote_options",
               "employment_type", "occupational_fields", "industries",
               "organisations", "source_path"]


def is_text(v):
    return isinstance(v, str) and v.strip() != "" and v.strip().lower() != "nan"


def clean_single(v):
    """Trim a single-value cell; blank/NaN -> ''. Not split."""
    return v.strip() if is_text(v) else ""


def split_terms(v, pattern=None):
    """Split a multi-value cell into clean, de-duplicated, order-preserved terms."""
    if not is_text(v):
        return []
    parts = pattern.split(v) if pattern else v.split(",")
    out, seen = [], set()
    for p in parts:
        t = p.strip()
        if t and t.lower() != "nan" and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def make_alert_id(row, idcols):
    raw = "\x1f".join("" if pd.isna(row[c]) else str(row[c]) for c in idcols)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xlsx", default="job-alert-export-1781539016.xlsx",
                    help="Path to the JGP job-alert export .xlsx")
    ap.add_argument("--out", default="job_alerts_path_clean.csv",
                    help="Output CSV path")
    args = ap.parse_args()

    df = pd.read_excel(args.xlsx, sheet_name=SHEET, dtype=str)
    total = len(df)
    idcols = [c for c in df.columns if c not in IDENTITY_EXCLUDE]

    # 1) Path alerts only (empty Search)
    path = df[~df["Search"].apply(is_text)].copy()
    n_path = len(path)

    # 2) Drop no-filter rows (the useless "/jobs" alerts)
    has_filter = path[FILTER_COLS].apply(lambda r: any(is_text(v) for v in r), axis=1)
    path = path[has_filter].copy()
    n_filtered = len(path)

    # 3) Stable id + dedupe (one row per alert)
    path["alert_id"] = path.apply(lambda r: make_alert_id(r, idcols), axis=1)
    path = path.drop_duplicates(subset="alert_id", keep="first").reset_index(drop=True)
    n_unique = len(path)

    # 4) Normalise Created -> ISO date only (yyyy-mm-dd); time lives in created_time.
    #    This export stores Created as a real Excel date, so pandas hands us an ISO
    #    datetime string ("2024-06-10 00:00:00"). Parse with that EXPLICIT format so
    #    there is no day/month guessing (dayfirst on an ISO string corrupts it); the
    #    n_baddate canary below will loudly flag any future export whose format
    #    differs, instead of silently mis-parsing. ISO text round-trips cleanly
    #    through Google Sheets + BigQuery.
    parsed = pd.to_datetime(path["Created"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    path["created_date_iso"] = parsed.dt.strftime("%Y-%m-%d")
    n_baddate = int(path["created_date_iso"].isna().sum())
    path["created_date_iso"] = path["created_date_iso"].fillna("")

    # 5) Build the clean, exploded rows
    rows = []
    for _, r in path.iterrows():
        rows.append({
            "alert_id": r["alert_id"],
            "alert_type": clean_single(r["Type"]),
            "created_date": r["created_date_iso"],
            "created_time": clean_single(r["Time"]),
            "activated": clean_single(r["Activated"]),
            "notification_interval": clean_single(r["Notification interval"]),
            "location": clean_single(r["Location"]),
            "remote_options": clean_single(r["Remote options"]),
            "employment_type": clean_single(r["Employment type"]),
            "occupational_fields": " | ".join(split_terms(r["Occupational field"])),
            "industries": " | ".join(split_terms(r["Industry"])),
            "organisations": " | ".join(split_terms(r["Organisation name"], ORG_SPLIT)),
            "source_path": clean_single(r["Path"]),
        })

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLS)
        w.writeheader()
        w.writerows(rows)

    print(f"total alerts in export        : {total}")
    print(f"path alerts (empty Search)    : {n_path}")
    print(f"after dropping no-filter rows : {n_filtered}")
    print(f"unique alerts (deduped)       : {n_unique}")
    print(f"unparseable created dates     : {n_baddate}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
