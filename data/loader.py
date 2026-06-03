"""BigQuery data loading and caching functions."""

import os
import streamlit as st
import pandas as pd
from google.oauth2.service_account import Credentials
from google.cloud import bigquery
from google.api_core.exceptions import NotFound, BadRequest
from datetime import datetime, timedelta

# BigQuery names live in data/config.py (single source of truth — points at the
# JPD Gold tables). Imported here so the query code below is otherwise unchanged.
from data.config import (
    BQ_PROJECT_ID,
    BQ_DATASET_ID,
    BQ_TABLE_ID,
    BQ_DAILY_TOTALS_TABLE_ID,
    BQ_REGION_SUMMARY_TABLE_ID,
    BQ_MEDIA_SUMMARY_TABLE_ID,
)

SCOPES = [
    'https://www.googleapis.com/auth/bigquery',
]


@st.cache_resource(ttl=None)
def get_bigquery_client():
    """Initialize and cache the BigQuery client."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # service_account.json is in the project root, one level up from data/
    project_root = os.path.dirname(script_dir)
    service_account_path = os.path.join(project_root, 'service_account.json')

    try:
        use_secrets = False
        try:
            if hasattr(st, 'secrets') and 'gcp_service_account' in st.secrets:
                use_secrets = True
        except Exception:
            use_secrets = False

        if use_secrets:
            creds = Credentials.from_service_account_info(
                st.secrets['gcp_service_account'],
                scopes=SCOPES
            )
        else:
            if not os.path.exists(service_account_path):
                st.error("No authentication found!")
                st.error(f"Local file does not exist at: {service_account_path}")
                st.error("Please either:")
                st.error("1. Add secrets to Streamlit Cloud (Settings > Secrets), OR")
                st.error("2. Add service_account.json file to the app directory")
                st.stop()

            creds = Credentials.from_service_account_file(
                service_account_path,
                scopes=SCOPES
            )

        client = bigquery.Client(credentials=creds, project=BQ_PROJECT_ID)
        return client
    except FileNotFoundError as e:
        st.error(f"Service account credentials not found at: {service_account_path}")
        st.error("Please add them to Streamlit secrets or place service_account.json in the app directory")
        st.stop()
    except Exception as e:
        st.error(f"Unexpected error initializing BigQuery client: {type(e).__name__}")
        st.error(f"Error message: {str(e)}")
        st.stop()


@st.cache_data(ttl=14400)
def load_all_data(days_back=None, sample_size=None):
    """Load vacancy, daily totals, region-exploded, and media summaries from BigQuery.

    Returns:
        (vacancy_df, daily_df, region_df, media_df) — region_df and media_df are
        None if their respective tables don't exist yet; consumers handle None
        (region: pipe-split fallback in views; media: section hidden in client report).
    """
    try:
        client = get_bigquery_client()
        if days_back is not None:
            cutoff_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
            vacancy_where = f"WHERE last_event_date >= '{cutoff_date}'"
            daily_where = f"WHERE event_date >= '{cutoff_date}'"
        else:
            vacancy_where = ""
            daily_where = ""
        limit_clause = f"LIMIT {sample_size}" if sample_size else ""

        # Core fields always present in t06_summary_vacancy
        core_fields = """
            entity_id_str,
            first_event_date,
            last_event_date,
            clicks,
            applies,
            title,
            organization_name,
            uk_regions,
            primary_uk_region,
            occupational_fields,
            importer_ID,
            importer_name,
            workflow_state,
            upgrades,
            start_date,
            end_date,
            category,
            contract_type,
            employment_type"""

        # Salary + sites fields (present once t06_summary_vacancy carries them)
        salary_fields = """,
            min_salary,
            max_salary,
            currency_code,
            salary_free_text,
            salary_exact,
            salary_unit,
            sites"""

        vacancy_query = f"""
        SELECT {core_fields}{salary_fields}
        FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_TABLE_ID}`
        {vacancy_where}
        {limit_clause}
        """

        # Enhanced daily query with GSC + site-split columns
        daily_query_full = f"""
        SELECT
            event_date,
            impressions, impressions_jgp, impressions_lg,
            gb_impressions_jgp, gb_impressions_lg,
            gsc_clicks, gsc_clicks_jgp, gsc_clicks_lg,
            gb_gsc_clicks_jgp, gb_gsc_clicks_lg,
            avg_position_jgp, avg_position_lg,
            job_listing_rich_jgp, job_listing_rich_lg,
            job_detail_rich_jgp, job_detail_rich_lg,
            clicks, clicks_jgp, clicks_lg,
            applies, applies_jgp, applies_lg,
            active_vacancies, active_jgp, active_lg
        FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_DAILY_TOTALS_TABLE_ID}`
        {daily_where}
        ORDER BY event_date
        """

        # Fallback daily query (pre-GSC schema)
        daily_query_basic = f"""
        SELECT
            event_date,
            clicks,
            applies,
            active_vacancies,
            active_jgp,
            active_lg
        FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_DAILY_TOTALS_TABLE_ID}`
        {daily_where}
        ORDER BY event_date
        """

        # Try with salary fields; fall back to core-only if table not yet updated
        try:
            vacancy_job = client.query(vacancy_query)
            vacancy_job.result()
        except (NotFound, BadRequest):
            # Salary/sites columns not in the table yet — fall back to core-only.
            # Narrow catch so transient/auth errors surface instead of silently
            # dropping columns.
            vacancy_query = f"""
            SELECT {core_fields}
            FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_TABLE_ID}`
            {vacancy_where}
            {limit_clause}
            """
            vacancy_job = client.query(vacancy_query)
            vacancy_job.result()

        # Try enhanced daily query; fall back to basic if table not yet updated
        try:
            daily_job = client.query(daily_query_full)
            daily_job.result()
        except (NotFound, BadRequest):
            # GSC/site-split columns not in the table yet — fall back to basic.
            daily_job = client.query(daily_query_basic)
            daily_job.result()

        vacancy_df = vacancy_job.to_dataframe(create_bqstorage_client=False)
        daily_df = daily_job.to_dataframe(create_bqstorage_client=False)

        # Region-exploded summary (one row per vacancy per region)
        # Falls back to None if the table doesn't exist yet
        region_df = None
        try:
            region_query = f"""
            SELECT
                entity_id_str,
                external_id,
                uk_region,
                raw_location,
                town_city,
                first_event_date,
                last_event_date,
                clicks,
                applies,
                title,
                organization_name,
                occupational_fields,
                importer_ID,
                importer_name,
                workflow_state,
                upgrades,
                start_date,
                end_date,
                category,
                contract_type,
                employment_type,
                min_salary,
                max_salary,
                currency_code,
                salary_free_text,
                salary_exact,
                salary_unit,
                sites
            FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_REGION_SUMMARY_TABLE_ID}`
            {vacancy_where}
            """
            region_job = client.query(region_query)
            region_job.result()
            region_df = region_job.to_dataframe(create_bqstorage_client=False)
        except (NotFound, BadRequest):
            pass  # Table/columns not present yet — views fall back to pipe-split
                  # logic. Narrow catch: transient/infra errors should surface
                  # (outer handler), not silently degrade.

        # Media-source breakdown per vacancy (one row per source/medium/campaign)
        # Falls back to None if the table doesn't exist yet
        media_df = None
        try:
            media_query = f"""
            SELECT
                entity_id_str,
                importer_ID,
                importer_name,
                source,
                medium,
                campaign,
                clicks,
                applies
            FROM `{BQ_PROJECT_ID}.{BQ_DATASET_ID}.{BQ_MEDIA_SUMMARY_TABLE_ID}`
            """
            media_job = client.query(media_query)
            media_job.result()
            media_df = media_job.to_dataframe(create_bqstorage_client=False)
        except (NotFound, BadRequest):
            pass  # Table/columns not present yet — client report Media section
                  # hides. Narrow catch so transient/infra errors surface.

        return vacancy_df, daily_df, region_df, media_df
    except Exception as e:
        st.error(f"Error loading data: {str(e)}")
        st.markdown(f"""
        **Troubleshooting:**
        - Check BigQuery tables exist in `{BQ_PROJECT_ID}.{BQ_DATASET_ID}`: `{BQ_TABLE_ID}` and `{BQ_DAILY_TOTALS_TABLE_ID}`
        - Verify service account has `bigquery.jobs.create` permission
        - Check the tables have data for the requested date range
        """)
        st.stop()


@st.cache_data(ttl=14400)
def get_data_loaded_at():
    """Wall-clock time the data cache was last populated. Same TTL as
    load_all_data (and cleared together by the Refresh button), so it reflects
    the last actual BigQuery fetch — not the current render time."""
    return datetime.now()


@st.cache_data(ttl=300)
def load_importer_mapping():
    """Load importer mapping from CSV file."""
    try:
        # Look for CSV in project root
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        csv_path = os.path.join(project_root, 'importer_mapping.csv')

        mapping_df = pd.read_csv(csv_path, encoding='utf-8-sig')
        if 'importer_id' in mapping_df.columns and 'importer_name' in mapping_df.columns:
            mapping_df = mapping_df[mapping_df['importer_id'].notna()]
            mapping_df = mapping_df[mapping_df['importer_id'].astype(str).str.strip() != '']
            importer_mapping = dict(zip(
                mapping_df['importer_id'].astype(str).str.strip(),
                mapping_df['importer_name'].str.strip()
            ))
            return importer_mapping
        return {}
    except Exception as e:
        st.error(f"Error loading importer mapping: {e}")
        return {}
