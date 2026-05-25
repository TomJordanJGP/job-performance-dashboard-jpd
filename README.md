# Job Performance Dashboard

A comprehensive analytics dashboard for job board performance metrics.

## Features

- 📊 Overview Dashboard with quartile performance analysis
- 🔍 Deep Dive analysis by Importer, Region, and Occupation
- 📋 Vacancy Performance tracking
- ⚖️ Side-by-side comparison tool
- 📈 Robust statistical metrics (Median & Mean with IQR outlier removal)

## Deployment

This dashboard is deployed on Streamlit Community Cloud.

### Local Development

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Add credentials:
   - Place `service_account.json` in root directory
   - Place `jobs-export.csv` in root directory

3. Run:
```bash
streamlit run app.py
```

## Data Sources

- BigQuery: Job events data
- CSV Export: Job metadata (titles, status, occupations, locations)

## Metrics Explained

- **Median Clicks/Vacancy**: Typical performance (robust to outliers)
- **Mean Clicks/Vacancy (IQR)**: Average after removing statistical outliers
- **Quartiles**: Performance distribution (Top 25%, Middle 50%, Bottom 25%)
