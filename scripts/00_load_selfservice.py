"""One-off: load manually_created_vacancies.csv → JPD.t01_feed_selfservice.

Why this exists separately from the live feeds:
  Self-service vacancies are user-created jobs on JGP that never go through
  an ATS / Scrape / CS export. They have a GA4-trackable entity_id (job_id)
  but no Jobiqo external_id hash. They're typically 'unpublished' state
  so they never appear in the live Appcast XML feed either.

  Yet GA4 has events for ~1,558 of them — these are the orphan events in
  t05. Loading this CSV resolves ~98% of current orphans, plus adds 3,576
  zero-traffic vacancies as new metadata_only rows in t05.

Schema design notes:
  * Mirrors the source-feed shape (carries salary, occupation, etc.) rather
    than the Appcast-overlay shape, because the CSV has the full data.
  * `organization_type` column holds the CSV's "Employer type (Industry)"
    value — populated on 92% of CSV rows and matches t04_organisations'
    canonical 9 industries exactly. Flows into t02's industry derivation
    as the same tier-3 fallback used by ATS/Scrape/Backfill feeds.
  * `external_id` stays NULL by design (CSV's "External ID" column is
    empty). t02 build treats self-service rows as a 4th segment (entity_id
    keyed, like Appcast-only).
  * `locations` is loaded as a STRING (raw CSV value, e.g. "London, UK").
    No nested STRUCT — t02 doesn't currently parse this layer, and the
    region resolution work (Phase 6) will handle parsing centrally.

Re-runnable: WRITE_TRUNCATE.
"""
import csv
from google.cloud import bigquery
from google.oauth2.service_account import Credentials

CSV_PATH = 'manually_created_vacancies.csv'
TABLE_ID = 'site-monitoring-421401.JPD.t01_feed_selfservice'
STAGING_TABLE = 'site-monitoring-421401.JPD._tmp_selfservice_ids'


def _to_float(v):
    v = (v or '').strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _to_ts(v):
    """CSV datetime is 'YYYY-MM-DD HH:MM:SS' — return as-is, BQ parses it as TIMESTAMP."""
    v = (v or '').strip()
    return v if v else None


def main():
    creds = Credentials.from_service_account_file(
        'service_account.json',
        scopes=['https://www.googleapis.com/auth/bigquery'],
    )
    client = bigquery.Client(credentials=creds, project='site-monitoring-421401', location='EU')

    # Drop the staging table from the earlier overlap analysis
    try:
        client.delete_table(STAGING_TABLE)
        print(f'Cleaned up staging table {STAGING_TABLE}')
    except Exception:
        pass

    rows = []
    with open(CSV_PATH, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            entity_id = (r['job_id'] or '').strip()
            if not entity_id:
                continue  # safety — every CSV row should have job_id
            rows.append({
                'entity_id':                (entity_id),
                'external_id':              (r['External ID'] or '').strip() or None,
                'title':                    (r['title'] or '').strip() or None,
                'organization_id':          (r['organization_id'] or '').strip() or None,
                'organization_name':        (r['organization_profile_name'] or '').strip() or None,
                'organization_type':        (r['Employer type (Industry)'] or '').strip() or None,
                'occupation':               (r['occupational_fields'] or '').strip() or None,
                'working_pattern':          (r['employment_type'] or '').strip() or None,
                'salary_min':               _to_float(r['min_salary']),
                'salary_max':               _to_float(r['max_salary']),
                'salary_exact':             _to_float(r['salary']),
                'salary_free_text':         None,  # not present in CSV
                'salary_type':              (r['salary_unit'] or '').strip() or None,
                'salary_currency':          (r['currency_code'] or '').strip() or None,
                'start_date':               _to_ts(r['publishing_date']),
                'close_date':               _to_ts(r['expiration_date']),
                'workflow_state':           (r['workflow_state'] or '').strip() or None,
                'application_workflow':     (r['application_workflow'] or '').strip() or None,
                'url':                      (r['job_url'] or '').strip() or None,
                'logo_url':                 (r['Logo'] or '').strip() or None,
                'locations':                (r['locations'] or '').strip() or None,
                'jgp_external_vacancy_id':  (r['jgp_external_vacancy_id'] or '').strip() or None,
            })

    print(f'Parsed {len(rows):,} rows from {CSV_PATH}')

    schema = [
        bigquery.SchemaField('entity_id',               'STRING', mode='REQUIRED'),
        bigquery.SchemaField('external_id',             'STRING'),
        bigquery.SchemaField('title',                   'STRING'),
        bigquery.SchemaField('organization_id',         'STRING'),
        bigquery.SchemaField('organization_name',       'STRING'),
        bigquery.SchemaField('organization_type',       'STRING'),
        bigquery.SchemaField('occupation',              'STRING'),
        bigquery.SchemaField('working_pattern',         'STRING'),
        bigquery.SchemaField('salary_min',              'FLOAT64'),
        bigquery.SchemaField('salary_max',              'FLOAT64'),
        bigquery.SchemaField('salary_exact',            'FLOAT64'),
        bigquery.SchemaField('salary_free_text',        'STRING'),
        bigquery.SchemaField('salary_type',             'STRING'),
        bigquery.SchemaField('salary_currency',         'STRING'),
        bigquery.SchemaField('start_date',              'TIMESTAMP'),
        bigquery.SchemaField('close_date',              'TIMESTAMP'),
        bigquery.SchemaField('workflow_state',          'STRING'),
        bigquery.SchemaField('application_workflow',    'STRING'),
        bigquery.SchemaField('url',                     'STRING'),
        bigquery.SchemaField('logo_url',                'STRING'),
        bigquery.SchemaField('locations',               'STRING'),
        bigquery.SchemaField('jgp_external_vacancy_id', 'STRING'),
    ]

    table_ref = bigquery.Table(TABLE_ID, schema=schema)
    table_ref.clustering_fields = ['entity_id']
    table_ref.description = (
        'Self-service vacancies — user-created JGP jobs that never enter the '
        'live feeds. Loaded one-off from manually_created_vacancies.csv via '
        'scripts/00_load_selfservice.py. Feeds into t02 as a 4th UNION segment '
        '(entity_id-keyed; no external_id). organization_type holds the CSV\'s '
        '"Employer type (Industry)" value directly.'
    )

    client.delete_table(TABLE_ID, not_found_ok=True)
    client.create_table(table_ref)

    job = client.load_table_from_json(
        rows,
        TABLE_ID,
        job_config=bigquery.LoadJobConfig(
            schema=schema,
            write_disposition='WRITE_TRUNCATE',
        ),
    )
    job.result()
    table = client.get_table(TABLE_ID)
    print(f'Loaded {table.num_rows:,} rows into {TABLE_ID}')


if __name__ == '__main__':
    main()
