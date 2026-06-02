"""Single source of truth for the JPD pipeline's BigQuery names.

data/loader.py imports these so the dashboard reads from the JPD Gold tables
rather than the old prod dataset. The rest of the front-end (app.py, views/*)
consumes loader's dataframes, not these names — so repointing here repoints the
whole dashboard.
"""

BQ_PROJECT_ID = "site-monitoring-421401"
BQ_DATASET_ID = "JPD"

# Gold summary tables the dashboard reads (built by scripts/06_create_summary_tables.sql).
BQ_TABLE_ID = "t06_summary_vacancy"                        # one row per vacancy
BQ_DAILY_TOTALS_TABLE_ID = "t06_summary_daily_totals"      # one row per day
BQ_REGION_SUMMARY_TABLE_ID = "t06_summary_vacancy_region"  # one row per (vacancy x region)
BQ_MEDIA_SUMMARY_TABLE_ID = "t06_summary_media"            # one row per (vacancy x source/medium/campaign)
