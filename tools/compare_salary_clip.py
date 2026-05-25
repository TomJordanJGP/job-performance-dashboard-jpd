"""Side-by-side comparison: current full-range salary histogram vs P02-P98 clipped.

Pulls the Kent County Council / Social Care slice for the last 12 months,
applies the same processing the dashboard uses (data/processing.py), then
renders both views into a single PNG so we can compare before committing
to a clipping strategy in views/client_report.py.

Uses plotly + kaleido (already in requirements.txt) so the styling matches
the live dashboard chart this PoC replaces.

Run:
    venv/bin/python tools/compare_salary_clip.py
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('GOOGLE_APPLICATION_CREDENTIALS', str(ROOT / 'service_account.json'))

from google.cloud import bigquery  # noqa: E402

from data.processing import add_occupation_column, process_salary_columns  # noqa: E402
from theme.colors import JGP_COLORS, JGP_PLOTLY_TEMPLATE  # noqa: E402

PROJECT = 'site-monitoring-421401'
DATASET = 'job_data_export'
CLIENT_NAME = 'Kent County Council'
OCC_LABEL = 'Social Care'
DAYS_BACK = 365


def main():
    cutoff = (datetime.now() - timedelta(days=DAYS_BACK)).strftime('%Y-%m-%d')
    bq = bigquery.Client(project=PROJECT, location='EU')

    region_q = f"""
    SELECT region
    FROM `{PROJECT}.{DATASET}.client_hq_addresses`
    WHERE LOWER(TRIM(organisation_name)) = LOWER(TRIM('{CLIENT_NAME}'))
    LIMIT 1
    """
    region_rows = list(bq.query(region_q).result())
    client_region = region_rows[0]['region'] if region_rows else None
    print(f"HQ region for {CLIENT_NAME}: {client_region}")

    data_q = f"""
    SELECT
        organization_name,
        occupational_fields,
        primary_uk_region,
        min_salary,
        max_salary,
        currency_code,
        salary_unit
    FROM `{PROJECT}.{DATASET}.dashboard_vacancy_summary`
    WHERE last_event_date >= '{cutoff}'
      AND (min_salary IS NOT NULL OR max_salary IS NOT NULL)
    """
    df = bq.query(data_q).to_dataframe(create_bqstorage_client=False)
    print(f"Pulled {len(df):,} rows with at least one salary bound")

    df = add_occupation_column(df)
    df = process_salary_columns(df)
    df = df[df['has_salary_data'] == True].copy()
    print(f"After annualisation: {len(df):,} rows have annual_mid_salary")

    kent = df[df['organization_name'].str.lower().str.strip() == CLIENT_NAME.lower()]
    print(f"\nKent CC vacancies with salary data: {len(kent):,}")
    print("Top 15 Kent CC occupations:")
    print(kent['occupation'].value_counts().head(15).to_string())

    if OCC_LABEL in df['occupation'].values:
        occ = OCC_LABEL
    else:
        candidates = df['occupation'][
            df['occupation'].str.contains(OCC_LABEL, case=False, na=False)
        ].unique()
        if len(candidates) == 0:
            print(f"\nNo occupation matches '{OCC_LABEL}'. Aborting.")
            return
        occ = candidates[0]
        print(f"\nUsing occupation '{occ}' (closest match to '{OCC_LABEL}')")

    occ_df = df[df['occupation'] == occ]
    market_salaries = occ_df['annual_mid_salary'].dropna()
    print(f"\nMarket {occ} salary data: n={len(market_salaries):,}")

    client_subset = kent[kent['occupation'] == occ]['annual_mid_salary'].dropna()
    client_n = len(client_subset)
    client_mean = client_subset.mean() if client_n else np.nan

    national_mean = market_salaries.mean()

    if client_region:
        norm = client_region.strip().lower()
        regional_subset = occ_df[
            occ_df['primary_uk_region'].fillna('').str.strip().str.lower() == norm
        ]['annual_mid_salary'].dropna()
    else:
        regional_subset = pd.Series([], dtype=float)
    regional_n = len(regional_subset)
    regional_mean = regional_subset.mean() if regional_n >= 3 else np.nan

    print(f"Kent CC mean: £{client_mean:,.0f} (n={client_n})")
    print(f"National mean: £{national_mean:,.0f} (n={len(market_salaries):,})")
    if not np.isnan(regional_mean):
        print(f"Regional mean ({client_region}): £{regional_mean:,.0f} (n={regional_n})")
    else:
        print(f"Regional mean: insufficient n ({regional_n})")

    p02, p98 = np.percentile(market_salaries, [2, 98])
    full_min, full_max = market_salaries.min(), market_salaries.max()
    n_trim = int(((market_salaries < p02) | (market_salaries > p98)).sum())
    print(f"\nFull range: £{full_min:,.0f} – £{full_max:,.0f}")
    print(f"P02–P98:    £{p02:,.0f} – £{p98:,.0f}  ({n_trim} outside range)")

    bar_color = JGP_COLORS['primary']
    client_c = JGP_COLORS['pink']
    national_c = JGP_COLORS['accent']
    regional_c = JGP_COLORS['deep_blue']

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=(
            f"Current — full range  (n={len(market_salaries):,} · "
            f"£{full_min:,.0f}–£{full_max:,.0f})",
            f"Proposed — P02–P98 clipped  (middle 96% · "
            f"{n_trim} outliers trimmed)",
        ),
        horizontal_spacing=0.10,
    )

    def add_panel(col, data, xlim, legend):
        fig.add_trace(
            go.Histogram(
                x=data, nbinsx=25,
                marker_color=bar_color, opacity=0.85,
                showlegend=False,
                hovertemplate='Salary: £%{x:,.0f}<br>Vacancies: %{y}<extra></extra>',
            ),
            row=1, col=col,
        )
        if not np.isnan(client_mean):
            fig.add_vline(x=client_mean, line=dict(color=client_c, width=2.5),
                          row=1, col=col)
        fig.add_vline(x=national_mean, line=dict(color=national_c, width=2.5),
                      row=1, col=col)
        if not np.isnan(regional_mean):
            fig.add_vline(x=regional_mean, line=dict(color=regional_c, width=2.5),
                          row=1, col=col)
        if xlim:
            fig.update_xaxes(range=list(xlim), row=1, col=col)
        if legend:
            if not np.isnan(client_mean):
                fig.add_trace(
                    go.Scatter(x=[None], y=[None], mode='lines',
                               line=dict(color=client_c, width=2.5),
                               name=f"Your mean £{client_mean:,.0f}"),
                    row=1, col=col,
                )
            fig.add_trace(
                go.Scatter(x=[None], y=[None], mode='lines',
                           line=dict(color=national_c, width=2.5),
                           name=f"National mean £{national_mean:,.0f}"),
                row=1, col=col,
            )
            if not np.isnan(regional_mean):
                fig.add_trace(
                    go.Scatter(x=[None], y=[None], mode='lines',
                               line=dict(color=regional_c, width=2.5),
                               name=f"{client_region} mean £{regional_mean:,.0f}"),
                    row=1, col=col,
                )

    add_panel(1, market_salaries, None, legend=True)

    means = [m for m in (client_mean, national_mean, regional_mean) if not np.isnan(m)]
    lo = min(p02, *means)
    hi = max(p98, *means)
    pad = (hi - lo) * 0.05
    xlim = (lo - pad, hi + pad)
    clipped = market_salaries[(market_salaries >= p02) & (market_salaries <= p98)]
    add_panel(2, clipped, xlim, legend=False)

    fig.update_layout(**JGP_PLOTLY_TEMPLATE['layout'])
    fig.update_layout(
        title=dict(
            text=(
                f"{occ} salary distribution — {CLIENT_NAME}, last 12 months"
            ),
            font=dict(size=18, color=JGP_COLORS['deep_blue']),
            x=0.02, xanchor='left', y=0.97,
        ),
        height=560,
        margin=dict(t=110, b=60, l=60, r=30),
        showlegend=True,
        legend=dict(
            orientation='h',
            yanchor='bottom', y=1.04,
            xanchor='left', x=0,
            font=dict(size=12),
        ),
        bargap=0.05,
    )
    fig.update_xaxes(tickprefix='£', tickformat=',')
    fig.update_xaxes(title_text="Annual mid salary", row=1, col=1)
    fig.update_xaxes(title_text="Annual mid salary", row=1, col=2)
    fig.update_yaxes(title_text="Vacancies", row=1, col=1)
    fig.update_yaxes(title_text="Vacancies", row=1, col=2)
    fig.update_annotations(font_size=13)

    out_dir = ROOT / 'outputs'
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f'salary_clip_comparison_{datetime.now().strftime("%Y%m%d_%H%M%S")}.png'
    fig.write_image(str(out), width=1600, height=620, scale=2)
    print(f"\nSaved: {out}")


if __name__ == '__main__':
    main()
