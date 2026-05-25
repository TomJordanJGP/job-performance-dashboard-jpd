"""Performance page - merged Deep Dive + Vacancy Performance."""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
from data.calculations import calculate_metrics, calculate_quartile_metrics, KPI_TOOLTIPS
from data.filters import apply_filters_to_data, apply_filters_to_region_data
from data.regions import get_country_for_region, COUNTRY_REGIONS
from theme.components import (
    kpi_card, page_header, filter_tags, section_header,
    branded_divider, empty_state,
)
from theme.colors import JGP_COLORS, JGP_PLOTLY_TEMPLATE, JGP_HEATMAP_COLORSCALE


def _fmt(val):
    """Format number: whole number with thousands separator."""
    return f"{int(round(val)):,}"


def render_performance(df, region_df=None):
    """Render the Performance page."""

    # Show active filter tags
    if st.session_state.get('global_filters'):
        st.markdown(filter_tags(st.session_state.global_filters), unsafe_allow_html=True)

    st.markdown(page_header("Performance"), unsafe_allow_html=True)

    # Apply global filters
    filtered_df = apply_filters_to_data(df, st.session_state.get('global_filters'))

    if len(filtered_df) == 0:
        st.markdown(empty_state("No data found for the selected filters. Try adjusting your filters.", "funnel"), unsafe_allow_html=True)
        return

    # KPI summary rows (6 cards with quartile breakdown)
    metrics = calculate_metrics(filtered_df)
    quartiles = calculate_quartile_metrics(filtered_df)

    def _quartile_val(key, metric):
        if not quartiles or key not in quartiles:
            return "N/A"
        return _fmt(quartiles[key][metric])

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(kpi_card(
            "Total Vacancies", _fmt(metrics['num_vacancies']),
            quartiles={
                'top_25': _quartile_val('top_25', 'num_vacancies'),
                'middle_50': _quartile_val('middle_50', 'num_vacancies'),
                'bottom_25': _quartile_val('bottom_25', 'num_vacancies'),
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Total Vacancies'],
        ), unsafe_allow_html=True)
    with col2:
        st.markdown(kpi_card(
            "Total Clicks", _fmt(metrics['total_clicks']),
            quartiles={
                'top_25': _quartile_val('top_25', 'total_clicks'),
                'middle_50': _quartile_val('middle_50', 'total_clicks'),
                'bottom_25': _quartile_val('bottom_25', 'total_clicks'),
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Total Clicks'],
        ), unsafe_allow_html=True)
    with col3:
        st.markdown(kpi_card(
            "Total Applies", _fmt(metrics['total_applies']),
            quartiles={
                'top_25': _quartile_val('top_25', 'total_applies'),
                'middle_50': _quartile_val('middle_50', 'total_applies'),
                'bottom_25': _quartile_val('bottom_25', 'total_applies'),
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Total Applies'],
        ), unsafe_allow_html=True)

    col4, col5, col6 = st.columns(3)
    with col4:
        st.markdown(kpi_card(
            "Apply/Click Rate", f"{round(metrics['apply_click_ratio'])}%",
            quartiles={
                'top_25': f"{round(quartiles['top_25']['apply_click_ratio'])}%",
                'middle_50': f"{round(quartiles['middle_50']['apply_click_ratio'])}%",
                'bottom_25': f"{round(quartiles['bottom_25']['apply_click_ratio'])}%",
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Apply/Click Rate'],
        ), unsafe_allow_html=True)
    with col5:
        st.markdown(kpi_card(
            "Avg Clicks / Vacancy", _fmt(metrics['mean_clicks_per_vacancy']),
            quartiles={
                'top_25': _fmt(quartiles['top_25']['clicks_per_vacancy']),
                'middle_50': _fmt(quartiles['middle_50']['clicks_per_vacancy']),
                'bottom_25': _fmt(quartiles['bottom_25']['clicks_per_vacancy']),
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Avg Clicks / Vacancy'],
        ), unsafe_allow_html=True)
    with col6:
        st.markdown(kpi_card(
            "Avg Applies / Vacancy", _fmt(metrics['mean_applies_per_vacancy']),
            quartiles={
                'top_25': _fmt(quartiles['top_25']['applies_per_vacancy']),
                'middle_50': _fmt(quartiles['middle_50']['applies_per_vacancy']),
                'bottom_25': _fmt(quartiles['bottom_25']['applies_per_vacancy']),
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Avg Applies / Vacancy'],
        ), unsafe_allow_html=True)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # === BENCHMARK TABLE ===
    st.markdown(section_header("Benchmark Comparison", "bar-chart-line"), unsafe_allow_html=True)

    dimension = st.selectbox(
        "Group by:",
        ['Occupation', 'Importer', 'Region', 'Company'],
        key='perf_dimension'
    )

    column_map = {
        'Importer': 'importer_name',
        'Region': 'uk_regions',
        'Occupation': 'occupation',
        'Company': 'organization_name'
    }

    col_name = column_map[dimension]
    if col_name in filtered_df.columns:
        benchmark_data = []

        if dimension == 'Region' and region_df is not None and 'uk_region' in region_df.columns:
            # Use pre-exploded region table
            filtered_region = apply_filters_to_region_data(region_df, st.session_state.get('global_filters'))
            for value in sorted(filtered_region['uk_region'].dropna().unique()):
                subset = filtered_region[filtered_region['uk_region'] == value]
                m = calculate_metrics(subset)
                benchmark_data.append({
                    dimension: value,
                    'Vacancies': m['num_vacancies'],
                    'Total Clicks': int(round(m['total_clicks'])),
                    'Total Applies': int(round(m['total_applies'])),
                    'Apply/Click %': round(m['apply_click_ratio']),
                    'Avg Clicks/Vac': round(m['mean_clicks_per_vacancy']),
                    'Avg Applies/Vac': round(m['mean_applies_per_vacancy']),
                })
        elif dimension == 'Region':
            # Fallback: pipe-split from vacancy summary
            all_values = set()
            for regions_str in filtered_df[col_name].dropna():
                for r in str(regions_str).split(' | '):
                    r = r.strip()
                    if r:
                        all_values.add(r)
            for value in all_values:
                mask = filtered_df[col_name].apply(
                    lambda x, v=value: v in [r.strip() for r in str(x).split(' | ')] if pd.notna(x) else False
                )
                subset = filtered_df[mask]
                m = calculate_metrics(subset)
                benchmark_data.append({
                    dimension: value,
                    'Vacancies': m['num_vacancies'],
                    'Total Clicks': int(round(m['total_clicks'])),
                    'Total Applies': int(round(m['total_applies'])),
                    'Apply/Click %': round(m['apply_click_ratio']),
                    'Avg Clicks/Vac': round(m['mean_clicks_per_vacancy']),
                    'Avg Applies/Vac': round(m['mean_applies_per_vacancy']),
                })
        else:
            for value in filtered_df[col_name].unique():
                subset = filtered_df[filtered_df[col_name] == value]
                m = calculate_metrics(subset)
                benchmark_data.append({
                    dimension: value,
                    'Vacancies': m['num_vacancies'],
                    'Total Clicks': int(round(m['total_clicks'])),
                    'Total Applies': int(round(m['total_applies'])),
                    'Apply/Click %': round(m['apply_click_ratio']),
                    'Avg Clicks/Vac': round(m['mean_clicks_per_vacancy']),
                    'Avg Applies/Vac': round(m['mean_applies_per_vacancy']),
                })

        if benchmark_data:
            benchmark_df = pd.DataFrame(benchmark_data).sort_values(dimension, ascending=True)
            st.dataframe(
                benchmark_df,
                width='stretch',
                hide_index=True,
                column_config={
                    'Vacancies': st.column_config.NumberColumn(format="%,d"),
                    'Total Clicks': st.column_config.NumberColumn(format="%,d"),
                    'Total Applies': st.column_config.NumberColumn(format="%,d"),
                    'Apply/Click %': st.column_config.NumberColumn(format="%d%%"),
                    'Avg Clicks/Vac': st.column_config.NumberColumn(format="%,d"),
                    'Avg Applies/Vac': st.column_config.NumberColumn(format="%,d"),
                },
            )

            csv = benchmark_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                "Download Benchmark Data",
                csv,
                f"benchmark_{dimension.lower()}_{datetime.now().strftime('%Y%m%d')}.csv",
                "text/csv"
            )
        else:
            st.info("No benchmark data available for the selected filters.")

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # === HEATMAP: Occupation x Region ===
    has_heatmap_data = (
        (region_df is not None and 'uk_region' in region_df.columns and 'occupation' in region_df.columns)
        or ('uk_regions' in filtered_df.columns and 'occupation' in filtered_df.columns)
    )
    if has_heatmap_data:
        st.markdown(section_header("Performance Heatmap (Occupation x Region)", "grid-3x3-gap"), unsafe_allow_html=True)

        heatmap_data = []

        if region_df is not None and 'uk_region' in region_df.columns:
            # Use pre-exploded region table
            filtered_region = apply_filters_to_region_data(region_df, st.session_state.get('global_filters'))
            for region in sorted(filtered_region['uk_region'].dropna().unique()):
                reg_subset = filtered_region[filtered_region['uk_region'] == region]
                for occ in reg_subset['occupation'].unique():
                    subset = reg_subset[reg_subset['occupation'] == occ]
                    if len(subset) > 0:
                        m = calculate_metrics(subset)
                        heatmap_data.append({
                            'Occupation': occ,
                            'Region': region,
                            'Clicks/Vacancy': round(m['clicks_per_vacancy']),
                            'Applies/Vacancy': round(m['applies_per_vacancy']),
                            'Apply/Click %': round(m['apply_click_ratio']),
                        })
        else:
            # Fallback: pipe-split from vacancy summary
            all_regions = set()
            for regions_str in filtered_df['uk_regions'].dropna():
                for r in str(regions_str).split(' | '):
                    r = r.strip()
                    if r:
                        all_regions.add(r)

            for region in all_regions:
                reg_mask = filtered_df['uk_regions'].apply(
                    lambda x, r=region: r in [rr.strip() for rr in str(x).split(' | ')] if pd.notna(x) else False
                )
                for occ in filtered_df['occupation'].unique():
                    subset = filtered_df[reg_mask & (filtered_df['occupation'] == occ)]
                    if len(subset) > 0:
                        m = calculate_metrics(subset)
                        heatmap_data.append({
                            'Occupation': occ,
                            'Region': region,
                            'Clicks/Vacancy': round(m['clicks_per_vacancy']),
                            'Applies/Vacancy': round(m['applies_per_vacancy']),
                            'Apply/Click %': round(m['apply_click_ratio']),
                        })

        if heatmap_data:
            heatmap_df = pd.DataFrame(heatmap_data)

            heatmap_metric = st.selectbox(
                "Select metric for heatmap:",
                ['Clicks/Vacancy', 'Applies/Vacancy', 'Apply/Click %'],
                key='perf_heatmap_metric'
            )

            heatmap_pivot = heatmap_df.pivot(index='Occupation', columns='Region', values=heatmap_metric)

            # Sort regions by country grouping
            sorted_regions = []
            for country, regions in COUNTRY_REGIONS.items():
                for r in regions:
                    if r in heatmap_pivot.columns:
                        sorted_regions.append(r)
            # Add any ungrouped regions
            for r in heatmap_pivot.columns:
                if r not in sorted_regions:
                    sorted_regions.append(r)
            heatmap_pivot = heatmap_pivot[sorted_regions]

            fig = px.imshow(
                heatmap_pivot,
                labels=dict(x="Region", y="Occupation", color=heatmap_metric),
                aspect="auto",
                color_continuous_scale=JGP_HEATMAP_COLORSCALE,
            )
            fig.update_layout(
                **JGP_PLOTLY_TEMPLATE['layout'],
                height=min(1200, max(400, len(heatmap_pivot) * 30)),
            )
            st.plotly_chart(fig, width='stretch')

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # === VACANCY TABLE ===
    st.markdown(section_header("Vacancy Table", "table"), unsafe_allow_html=True)

    job_col = 'entity_id' if 'entity_id' in filtered_df.columns else filtered_df.columns[0]

    # Pre-compute occupation averages for the filtered data
    occ_avg = {}
    if 'occupation' in filtered_df.columns and 'clicks' in filtered_df.columns:
        for occ in filtered_df['occupation'].unique():
            occ_subset = filtered_df[filtered_df['occupation'] == occ]
            n = len(occ_subset)
            if n > 0:
                occ_avg[occ] = {
                    'clicks': round(occ_subset['clicks'].sum() / n),
                    'applies': round(occ_subset['applies'].sum() / n),
                }

    vacancy_data = []
    for _, job in filtered_df.iterrows():
        job_id = job[job_col]
        clicks = int(job.get('clicks', 0))
        applies = int(job.get('applies', 0))
        ratio = round((applies / clicks * 100)) if clicks > 0 else 0

        status = job.get('workflow_state', 'Unknown')
        is_published = status == 'published'

        days_active = None
        start_date = job.get('start_date')
        end_date = job.get('end_date')
        if pd.notna(start_date):
            if pd.notna(end_date):
                days_active = (end_date - start_date).days
            elif is_published:
                today = pd.Timestamp(datetime.now())
                days_active = (today - start_date).days

        occupation = job.get('occupation', 'Unknown')
        if pd.isna(occupation) or not str(occupation).strip():
            occupation = 'Unknown'

        upgrades_str = ', '.join(job.get('upgrades_list', [])) if 'upgrades_list' in job.index else ''

        occ_clicks_avg = occ_avg.get(occupation, {}).get('clicks')
        occ_applies_avg = occ_avg.get(occupation, {}).get('applies')

        vacancy_data.append({
            'Title': job.get('title', 'Unknown'),
            'Company': job.get('organization_name', 'Unknown'),
            'Job ID': str(job_id),
            'Status': status,
            'Start Date': start_date if pd.notna(start_date) else None,
            'End Date': end_date if pd.notna(end_date) else None,
            'Days Active': int(days_active) if days_active is not None and days_active > 0 else None,
            'Region': job.get('uk_regions', 'Unknown'),
            'Occupation': occupation,
            'Importer': job.get('importer_name', 'Unknown'),
            'Upgrades': upgrades_str if upgrades_str else 'None',
            'Clicks': clicks,
            'Applies': applies,
            'Ratio %': ratio if clicks > 0 else None,
            'Clicks/Day': round(clicks / days_active) if days_active and days_active > 0 else None,
            'Applies/Day': round(applies / days_active) if days_active and days_active > 0 else None,
            'Avg Clicks/Vac (Occ)': occ_clicks_avg,
            'Avg Applies/Vac (Occ)': occ_applies_avg,
        })

    vacancy_df = pd.DataFrame(vacancy_data)

    if len(vacancy_df) == 0:
        st.markdown(empty_state("No vacancies found for the selected filters.", "inbox"), unsafe_allow_html=True)
        return

    vacancy_df = vacancy_df.sort_values('Clicks', ascending=False)

    st.caption(f"Showing {len(vacancy_df):,} vacancies")
    st.dataframe(
        vacancy_df,
        width='stretch',
        height=600,
        hide_index=True,
        column_config={
            'Clicks': st.column_config.NumberColumn(format="%,d"),
            'Applies': st.column_config.NumberColumn(format="%,d"),
            'Ratio %': st.column_config.NumberColumn(format="%d%%"),
            'Clicks/Day': st.column_config.NumberColumn(format="%,d"),
            'Applies/Day': st.column_config.NumberColumn(format="%,d"),
            'Avg Clicks/Vac (Occ)': st.column_config.NumberColumn(format="%,d"),
            'Avg Applies/Vac (Occ)': st.column_config.NumberColumn(format="%,d"),
        },
    )

    csv = vacancy_df.to_csv(index=False).encode('utf-8')
    st.download_button(
        "Download Vacancy Report",
        csv,
        f"vacancy_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        "text/csv"
    )
