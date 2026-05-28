"""One-off: load organizations-export.csv → JPD.t04_organisations.

The CSV is a per-organisation reference table exported from Jobiqo with the
organisation's profile-level industry. It powers the `industry` field on t02
and t05 (master source), with the parent/child/standard-stripped
`organization_type` from feed XML as fallback for orgs not in this table.

Columns kept (7 of 12 in source CSV):
  organization_id      <- "Organization ID"             (STRING)
  organisation_name    <- "Organisation name"           (STRING)
  industry             <- "Org. Profile: Industry"      (STRING — 9 distinct values)
  street               <- "Org. Profile: Address Street"
  postcode             <- "Org. Profile: Address Postal Code"
  city                 <- "Org. Profile: Address City"
  country              <- "Address Country"             (top-level — not the Org. Profile variant)

Columns dropped: LegacyID, top-level Address Street/Postal Code/City, and
Org. Profile: Address Country (kept the top-level country instead).

Re-run safely: uses WRITE_TRUNCATE so the table is fully replaced each time.
"""
import csv
import os
from google.cloud import bigquery
from google.oauth2.service_account import Credentials

CSV_PATH = 'organizations-export.csv'
TABLE_ID = 'site-monitoring-421401.JPD.t04_organisations'


def main():
    creds = Credentials.from_service_account_file(
        'service_account.json',
        scopes=['https://www.googleapis.com/auth/bigquery'],
    )
    client = bigquery.Client(credentials=creds, project='site-monitoring-421401', location='EU')

    rows = []
    with open(CSV_PATH, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for src in reader:
            rows.append({
                'organization_id':   (src['Organization ID'] or '').strip() or None,
                'organisation_name': (src['Organisation name'] or '').strip() or None,
                'industry':          (src['Org. Profile: Industry'] or '').strip() or None,
                'street':            (src['Org. Profile: Address Street'] or '').strip() or None,
                'postcode':          (src['Org. Profile: Address Postal Code'] or '').strip() or None,
                'city':              (src['Org. Profile: Address City'] or '').strip() or None,
                'country':           (src['Address Country'] or '').strip() or None,
            })

    print(f'Parsed {len(rows)} rows from {CSV_PATH}')

    schema = [
        bigquery.SchemaField('organization_id',   'STRING', mode='REQUIRED'),
        bigquery.SchemaField('organisation_name', 'STRING'),
        bigquery.SchemaField('industry',          'STRING'),
        bigquery.SchemaField('street',            'STRING'),
        bigquery.SchemaField('postcode',          'STRING'),
        bigquery.SchemaField('city',              'STRING'),
        bigquery.SchemaField('country',           'STRING'),
    ]

    table_ref = bigquery.Table(TABLE_ID, schema=schema)
    table_ref.clustering_fields = ['organization_id']
    table_ref.description = (
        'Per-organisation reference table (Jobiqo export). Master source for '
        'the `industry` field on t02/t05; cluster key is organization_id. '
        'Re-loaded by scripts/00_load_organisations.py (idempotent — '
        'WRITE_TRUNCATE).'
    )

    # Drop + recreate to apply schema / clustering cleanly.
    client.delete_table(TABLE_ID, not_found_ok=True)
    client.create_table(table_ref)

    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition='WRITE_TRUNCATE',
    )
    job = client.load_table_from_json(rows, TABLE_ID, job_config=job_config)
    job.result()

    table = client.get_table(TABLE_ID)
    print(f'Loaded {table.num_rows:,} rows into {TABLE_ID}')


if __name__ == '__main__':
    main()
