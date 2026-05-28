"""One-off: load the ONS Postcode Directory (May 2024) → JPD.t04_postcodes.

Source: Postcodes_ONSPD_MAY_2024/Data/ONSPD_MAY_2024_UK.txt (1.1 GB, ~2.7M rows).
ONS publishes a flat CSV with 53 columns; we keep only 7 (postcode + 5 geography
codes + termination date), resolve the codes to human-readable names via 5 small
lookup files in Postcodes_ONSPD_MAY_2024/Documents/, and upload the clean
~2.7M-row table to BQ.

This powers Tier 1 (postcode → UK region) and Tier 2 (city/town → region) of
the region-resolution cascade used by t02_job_table + t05_enriched_vacancies.

Columns in t04_postcodes:
  postcode         STRING NOT NULL   -- in "M1 1AA" format (the `pcds` field)
  postcode_area    STRING            -- "M", "EC", "SW1" — useful for crude grouping
  lad_code         STRING            -- ONS LAD code (e.g. "E08000003")
  lad_name         STRING            -- "Manchester"
  city_code        STRING            -- BUA13 code (e.g. "E34005054")
  city_name        STRING            -- "Manchester" (BUA suffix stripped)
  town_code        STRING            -- BUASD13 code
  town_name        STRING            -- "Stretford" (BUASD suffix stripped)
  region_code      STRING            -- RGN20 code (English postcodes only)
  region_name      STRING            -- "North West" — populated for England
  country_code     STRING            -- CTRY12 code
  country_name     STRING            -- "England" / "Wales" / "Scotland" / "Northern Ireland"
  date_introduced  STRING            -- YYYYMM
  date_terminated  STRING            -- YYYYMM if retired, else NULL

Re-runnable: WRITE_TRUNCATE. Reads source files in place — no caching.
"""
import csv
import os
import re
import sys
import tempfile
import time
from google.cloud import bigquery
from google.oauth2.service_account import Credentials

ONSPD_DIR = 'Postcodes_ONSPD_MAY_2024'
# The single ONSPD_MAY_2024_UK.txt file is fixed-width and inconvenient. ONS
# publishes the same data in CSV form split by postcode area (~126 files) in
# Data/multi_csv/. We iterate all of them.
ONSPD_CSV_DIR = os.path.join(ONSPD_DIR, 'Data', 'multi_csv')
DOCS_DIR = os.path.join(ONSPD_DIR, 'Documents')
TABLE_ID = 'site-monitoring-421401.JPD.t04_postcodes'

LOOKUPS = {
    'lad':     (os.path.join(DOCS_DIR, 'LAD23_LAU121_ITL321_ITL221_ITL121_UK_LU.csv'),
                'LAD23CD', 'LAD23NM'),
    'bua':     (os.path.join(DOCS_DIR, 'BUA_names and codes UK as at 12_13.csv'),
                'BUA13CD', 'BUA13NM'),
    'buasd':   (os.path.join(DOCS_DIR, 'BUASD_names and codes UK as at 12_13.csv'),
                'BUASD13CD', 'BUASD13NM'),
    'rgn':     (os.path.join(DOCS_DIR, 'Region names and codes EN as at 12_20 (RGN).csv'),
                'RGN20CD', 'RGN20NM'),
    'country': (os.path.join(DOCS_DIR, 'Country names and codes UK as at 08_12.csv'),
                'CTRY12CD', 'CTRY12NM'),
}


def load_lookup(path, code_col, name_col):
    """Load a small ONS code-to-name CSV into a dict. Strips UTF-8 BOM."""
    d = {}
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = (row.get(code_col) or '').strip()
            name = (row.get(name_col) or '').strip()
            if code and name:
                d[code] = name
    return d


def strip_suffix(name, suffix):
    """Strip a trailing word like " BUA" or " BUASD" from a name."""
    if not name:
        return name
    pattern = r'\s+' + re.escape(suffix) + r'\s*$'
    return re.sub(pattern, '', name).strip()


_POSTCODE_AREA_RE = re.compile(r'^([A-Z]{1,2})')


def postcode_area(pcds):
    """Extract the area prefix (1-2 letters) from a postcode like 'M1 1AA' -> 'M'."""
    m = _POSTCODE_AREA_RE.match((pcds or '').strip().upper())
    return m.group(1) if m else None


def main():
    creds = Credentials.from_service_account_file(
        'service_account.json',
        scopes=['https://www.googleapis.com/auth/bigquery'],
    )
    client = bigquery.Client(credentials=creds, project='site-monitoring-421401', location='EU')

    # 1) Load all 5 lookup tables
    print('Loading lookup tables...')
    lookups = {}
    for key, (path, code_col, name_col) in LOOKUPS.items():
        if not os.path.exists(path):
            sys.exit(f'  Missing lookup file: {path}')
        lookups[key] = load_lookup(path, code_col, name_col)
        print(f'  {key:<8}  {len(lookups[key]):>6,} entries')

    if not os.path.isdir(ONSPD_CSV_DIR):
        sys.exit(f'Missing ONSPD CSV directory: {ONSPD_CSV_DIR}')

    csv_files = sorted(
        os.path.join(ONSPD_CSV_DIR, f)
        for f in os.listdir(ONSPD_CSV_DIR)
        if f.startswith('ONSPD_') and f.endswith('.csv')
    )
    if not csv_files:
        sys.exit(f'No ONSPD CSV files found in {ONSPD_CSV_DIR}')
    print(f'\nFound {len(csv_files)} ONSPD CSV files to stream')

    # 2) Stream each per-area CSV, transform rows, write a single clean CSV.
    #    Streaming keeps memory bounded — never hold all 2.7M rows in memory.
    out_fd, out_path = tempfile.mkstemp(prefix='t04_postcodes_', suffix='.csv')
    os.close(out_fd)

    print(f'Streaming -> {out_path}')
    t0 = time.time()
    n_in = 0
    n_out = 0

    output_cols = [
        'postcode', 'postcode_area',
        'lad_code', 'lad_name',
        'city_code', 'city_name',
        'town_code', 'town_name',
        'region_code', 'region_name',
        'country_code', 'country_name',
        'date_introduced', 'date_terminated',
    ]

    with open(out_path, 'w', encoding='utf-8', newline='') as fout:
        writer = csv.DictWriter(fout, fieldnames=output_cols)
        writer.writeheader()

        for csv_path in csv_files:
            with open(csv_path, encoding='utf-8-sig', newline='') as fin:
                reader = csv.DictReader(fin)
                for row in reader:
                    n_in += 1
                    pcds = (row.get('pcds') or '').strip()
                    if not pcds:
                        continue  # postcode is the primary key — drop NULL ones

                    lad_code   = (row.get('oslaua') or '').strip()
                    bua_code   = (row.get('bua11')  or '').strip()
                    buasd_code = (row.get('buasd11') or '').strip()
                    rgn_code   = (row.get('rgn')    or '').strip()
                    ctry_code  = (row.get('ctry')   or '').strip()
                    doterm     = (row.get('doterm') or '').strip()
                    dointr     = (row.get('dointr') or '').strip()

                    writer.writerow({
                        'postcode':        pcds,
                        'postcode_area':   postcode_area(pcds),
                        'lad_code':        lad_code or None,
                        'lad_name':        lookups['lad'].get(lad_code) or None,
                        'city_code':       bua_code or None,
                        'city_name':       strip_suffix(lookups['bua'].get(bua_code), 'BUA') or None,
                        'town_code':       buasd_code or None,
                        'town_name':       strip_suffix(lookups['buasd'].get(buasd_code), 'BUASD') or None,
                        'region_code':     rgn_code or None,
                        'region_name':     lookups['rgn'].get(rgn_code) or None,
                        'country_code':    ctry_code or None,
                        'country_name':    lookups['country'].get(ctry_code) or None,
                        'date_introduced': dointr or None,
                        'date_terminated': doterm or None,
                    })
                    n_out += 1
                    if n_in % 250_000 == 0:
                        print(f'  {n_in:>10,} rows read, {n_out:>10,} written ({time.time()-t0:.1f}s)')

    print(f'\nStreaming done: {n_in:,} read, {n_out:,} written ({time.time()-t0:.1f}s)')
    cleaned_size_mb = os.path.getsize(out_path) / 1e6
    print(f'Cleaned CSV: {cleaned_size_mb:.0f} MB')

    # 3) Create the BQ table with schema + clustering, then load the CSV
    schema = [
        bigquery.SchemaField('postcode',        'STRING', mode='REQUIRED'),
        bigquery.SchemaField('postcode_area',   'STRING'),
        bigquery.SchemaField('lad_code',        'STRING'),
        bigquery.SchemaField('lad_name',        'STRING'),
        bigquery.SchemaField('city_code',       'STRING'),
        bigquery.SchemaField('city_name',       'STRING'),
        bigquery.SchemaField('town_code',       'STRING'),
        bigquery.SchemaField('town_name',       'STRING'),
        bigquery.SchemaField('region_code',     'STRING'),
        bigquery.SchemaField('region_name',     'STRING'),
        bigquery.SchemaField('country_code',    'STRING'),
        bigquery.SchemaField('country_name',    'STRING'),
        bigquery.SchemaField('date_introduced', 'STRING'),
        bigquery.SchemaField('date_terminated', 'STRING'),
    ]
    table_ref = bigquery.Table(TABLE_ID, schema=schema)
    table_ref.clustering_fields = ['postcode']
    table_ref.description = (
        'UK postcode → administrative geography lookup, sourced from ONSPD May 2024. '
        'Powers the region-resolution cascade in t02 + t05. Re-loaded by '
        'scripts/00_load_postcodes.py.'
    )

    client.delete_table(TABLE_ID, not_found_ok=True)
    client.create_table(table_ref)

    print(f'\nUploading to {TABLE_ID}...')
    t0 = time.time()
    with open(out_path, 'rb') as f:
        job = client.load_table_from_file(
            f, TABLE_ID,
            job_config=bigquery.LoadJobConfig(
                schema=schema,
                source_format=bigquery.SourceFormat.CSV,
                skip_leading_rows=1,
                write_disposition='WRITE_TRUNCATE',
            ),
        )
    job.result()
    print(f'  Upload done in {time.time()-t0:.1f}s')

    table = client.get_table(TABLE_ID)
    print(f'  Final row count: {table.num_rows:,}')

    os.unlink(out_path)


if __name__ == '__main__':
    main()
