# Scripts Directory

This directory contains utility scripts for managing data and BigQuery tables.

## Upload Scripts

### upload_job_export_to_bq.py
Uploads job_export.csv data to BigQuery, replacing the existing table.

**Usage:**
```bash
# Use default path (data/job_export.csv)
python scripts/upload_job_export_to_bq.py

# Specify custom path
python scripts/upload_job_export_to_bq.py path/to/your/job_export.csv
```

**What it does:**
- Validates the CSV file exists and has data
- Uploads to `jgp-data-dev.jgp_recruitment.job_export`
- Replaces existing table data (WRITE_TRUNCATE)
- Auto-detects schema from CSV
- Shows upload progress and final table statistics

### upload_location_lookup_to_bq.py
Uploads location lookup data to BigQuery.

**Usage:**
```bash
python scripts/upload_location_lookup_to_bq.py
```

## Data Processing Scripts

### create_job_metadata_table.py
Creates the job metadata reference table in BigQuery.

### process_postcode_lookup.py
Processes postcode lookup data for location mapping.

### create_county_to_region_mapping.py
Creates county to UK region mapping table.

### add_regions_to_lookup.py
Adds regional information to the location lookup table.

## Requirements

All scripts require:
- Service account key: `jgp-data-dev-bq-key.json` in project root
- Python packages: `google-cloud-bigquery`, `pandas`

Install dependencies:
```bash
pip install google-cloud-bigquery pandas
```

## Notes

- All upload scripts use WRITE_TRUNCATE (replace existing data)
- Service account must have BigQuery Data Editor permissions
- Auto-detect schema is used for flexibility
