"""Dashboard (Overview) page - at-a-glance health check."""

import os
import json
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from data.calculations import calculate_metrics, calculate_quartile_metrics, KPI_TOOLTIPS
from data.filters import apply_filters_to_data, apply_filters_to_region_data
from data.regions import get_country_for_region, COUNTRY_REGIONS
from theme.components import kpi_card, page_header, filter_tags, section_header, branded_divider
from theme.colors import JGP_COLORS, JGP_PLOTLY_TEMPLATE, JGP_HEATMAP_COLORSCALE


def _fmt(val):
    """Format number: whole number with thousands separator."""
    return f"{int(round(val)):,}"


def _quartile_val(quartiles, key, metric):
    """Extract a quartile metric value, formatted."""
    if not quartiles or key not in quartiles:
        return "N/A"
    return _fmt(quartiles[key][metric])


def render_dashboard(df, daily_totals=None, region_df=None):
    """Render the Dashboard page."""

    # Show active filter tags
    if st.session_state.get('global_filters'):
        st.markdown(filter_tags(st.session_state.global_filters), unsafe_allow_html=True)

    # Page header
    st.markdown(page_header("Dashboard"), unsafe_allow_html=True)

    # Apply global filters
    filtered_df = apply_filters_to_data(df, st.session_state.get('global_filters'))

    # Calculate metrics
    metrics = calculate_metrics(filtered_df)
    quartiles = calculate_quartile_metrics(filtered_df)

    # KPI Cards - 6 cards: Vacancies, Clicks, Applies, Apply Rate, Avg Clicks/Vac, Avg Applies/Vac
    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(kpi_card(
            "Total Vacancies", _fmt(metrics['num_vacancies']),
            quartiles={
                'top_25': _quartile_val(quartiles, 'top_25', 'num_vacancies'),
                'middle_50': _quartile_val(quartiles, 'middle_50', 'num_vacancies'),
                'bottom_25': _quartile_val(quartiles, 'bottom_25', 'num_vacancies'),
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Total Vacancies'],
        ), unsafe_allow_html=True)
    with col2:
        st.markdown(kpi_card(
            "Total Clicks", _fmt(metrics['total_clicks']),
            quartiles={
                'top_25': _quartile_val(quartiles, 'top_25', 'total_clicks'),
                'middle_50': _quartile_val(quartiles, 'middle_50', 'total_clicks'),
                'bottom_25': _quartile_val(quartiles, 'bottom_25', 'total_clicks'),
            } if quartiles else None,
            tooltip=KPI_TOOLTIPS['Total Clicks'],
        ), unsafe_allow_html=True)
    with col3:
        st.markdown(kpi_card(
            "Total Applies", _fmt(metrics['total_applies']),
            quartiles={
                'top_25': _quartile_val(quartiles, 'top_25', 'total_applies'),
                'middle_50': _quartile_val(quartiles, 'middle_50', 'total_applies'),
                'bottom_25': _quartile_val(quartiles, 'bottom_25', 'total_applies'),
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

    # Trend chart
    if daily_totals is not None and len(daily_totals) > 0:
        st.markdown(section_header("Trends Over Time", "graph-up"), unsafe_allow_html=True)

        has_active_filters = False
        if st.session_state.get('global_filters'):
            f = st.session_state.global_filters
            if any([f.get('importer'), f.get('company'), f.get('region'),
                    f.get('occupation'), f.get('upgrades'), f.get('job_title'),
                    f.get('entity_id')]):
                has_active_filters = True

        if has_active_filters:
            st.caption("Trend data shows global site performance (not affected by filters).")

        trend_granularity = st.selectbox(
            "View by", ["Daily", "Weekly", "Monthly"], key='trend_granularity'
        )

        daily_data = daily_totals.copy()
        daily_data['event_date'] = pd.to_datetime(daily_data['event_date'])
        daily_data = daily_data.sort_values('event_date')

        if trend_granularity == 'Weekly':
            daily_data['period'] = daily_data['event_date'].dt.to_period('W').apply(lambda p: p.start_time)
            daily_data = daily_data.groupby('period', as_index=False).agg(
                clicks=('clicks', 'sum'),
                applies=('applies', 'sum'),
            ).rename(columns={'period': 'event_date'})
        elif trend_granularity == 'Monthly':
            daily_data['period'] = daily_data['event_date'].dt.to_period('M').apply(lambda p: p.start_time)
            daily_data = daily_data.groupby('period', as_index=False).agg(
                clicks=('clicks', 'sum'),
                applies=('applies', 'sum'),
            ).rename(columns={'period': 'event_date'})

        daily_data = daily_data.sort_values('event_date')

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=daily_data['event_date'],
            y=daily_data['clicks'],
            name='Clicks',
            line=dict(color=JGP_COLORS['primary'], width=2),
            fill='tozeroy',
            fillcolor='rgba(100, 55, 145, 0.08)',
        ))
        fig.add_trace(go.Scatter(
            x=daily_data['event_date'],
            y=daily_data['applies'],
            name='Applies',
            line=dict(color=JGP_COLORS['deep_green'], width=2),
            fill='tozeroy',
            fillcolor='rgba(46, 69, 0, 0.08)',
        ))
        fig.update_layout(
            **JGP_PLOTLY_TEMPLATE['layout'],
            height=400,
            hovermode='x unified',
            xaxis_title=None,
            yaxis_title=None,
        )
        st.plotly_chart(fig, width='stretch')

    # === VACANCY TREND BY SITE ===
    if daily_totals is not None and len(daily_totals) > 0:
        has_site_cols = all(c in daily_totals.columns for c in ['active_vacancies', 'active_jgp', 'active_lg'])
        if has_site_cols:
            st.markdown(branded_divider(), unsafe_allow_html=True)
            st.markdown(section_header("Live Vacancies Over Time", "building"), unsafe_allow_html=True)
            if has_active_filters:
                st.caption("Vacancy counts show global site totals (not affected by filters).")

            granularity = st.selectbox(
                "View by", ["Daily", "Weekly", "Monthly"], key='vacancy_trend_granularity'
            )

            trend_data = daily_totals.copy()
            trend_data['event_date'] = pd.to_datetime(trend_data['event_date'])
            trend_data = trend_data.sort_values('event_date')

            if granularity == 'Weekly':
                trend_data['period'] = trend_data['event_date'].dt.to_period('W').apply(lambda p: p.start_time)
                trend_data = trend_data.groupby('period', as_index=False).agg(
                    active_vacancies=('active_vacancies', 'mean'),
                    active_jgp=('active_jgp', 'mean'),
                    active_lg=('active_lg', 'mean'),
                ).rename(columns={'period': 'event_date'})
            elif granularity == 'Monthly':
                trend_data['period'] = trend_data['event_date'].dt.to_period('M').apply(lambda p: p.start_time)
                trend_data = trend_data.groupby('period', as_index=False).agg(
                    active_vacancies=('active_vacancies', 'mean'),
                    active_jgp=('active_jgp', 'mean'),
                    active_lg=('active_lg', 'mean'),
                ).rename(columns={'period': 'event_date'})

            trend_data = trend_data.sort_values('event_date')
            for col in ['active_vacancies', 'active_jgp', 'active_lg']:
                trend_data[col] = trend_data[col].round().astype(int)

            fig_vac = go.Figure()
            fig_vac.add_trace(go.Bar(
                x=trend_data['event_date'],
                y=trend_data['active_jgp'],
                name='Jobs Go Public',
                marker_color=JGP_COLORS['primary'],
            ))
            fig_vac.add_trace(go.Bar(
                x=trend_data['event_date'],
                y=trend_data['active_lg'],
                name='LG Jobs',
                marker_color=JGP_COLORS['deep_green'],
            ))
            fig_vac.add_trace(go.Scatter(
                x=trend_data['event_date'],
                y=trend_data['active_vacancies'],
                name='Total (deduplicated)',
                line=dict(color=JGP_COLORS['blue'], width=3),
                mode='lines',
            ))
            fig_vac.update_layout(
                **JGP_PLOTLY_TEMPLATE['layout'],
                height=450,
                barmode='stack',
                hovermode='x unified',
                xaxis_title=None,
                yaxis_title='Live Vacancies',
            )
            st.plotly_chart(fig_vac, width='stretch')

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # Job Listings by Country / Region
    has_region_data = (region_df is not None and 'uk_region' in region_df.columns) or 'uk_regions' in filtered_df.columns
    if has_region_data:
        st.markdown(section_header("Job Listings by Country / Region", "geo-alt"), unsafe_allow_html=True)
        st.caption(
            "A vacancy listed in multiple regions is counted once per region. "
            "Regional totals may therefore exceed the overall vacancy count."
        )

        if region_df is not None and 'uk_region' in region_df.columns:
            # Use pre-exploded region table — no pipe-splitting needed
            filtered_region = apply_filters_to_region_data(region_df, st.session_state.get('global_filters'))
            region_counts = filtered_region.groupby('uk_region').size().to_dict()
        else:
            # Fallback: pipe-split from vacancy summary
            region_counts = {}
            for regions_str in filtered_df['uk_regions'].dropna():
                for r in str(regions_str).split(' | '):
                    r = r.strip()
                    if r:
                        region_counts[r] = region_counts.get(r, 0) + 1

        if region_counts:
            # Build country-grouped data
            chart_data = []
            for country, regions in COUNTRY_REGIONS.items():
                for region in regions:
                    if region in region_counts:
                        chart_data.append({
                            'Country': country,
                            'Region': region,
                            'Vacancies': region_counts[region],
                        })

            if chart_data:
                chart_df = pd.DataFrame(chart_data)

                fig = go.Figure()
                colors = {
                    'England': JGP_COLORS['primary'],
                    'Scotland': JGP_COLORS['supporting'],
                    'Wales': JGP_COLORS['deep_green'],
                    'Northern Ireland': JGP_COLORS['blue'],
                }
                for country in chart_df['Country'].unique():
                    country_data = chart_df[chart_df['Country'] == country]
                    fig.add_trace(go.Bar(
                        x=country_data['Region'],
                        y=country_data['Vacancies'],
                        name=country,
                        marker_color=colors.get(country, JGP_COLORS['primary']),
                        text=country_data['Vacancies'].apply(lambda v: f"{v:,}"),
                        textposition='auto',
                    ))

                fig.update_layout(
                    **JGP_PLOTLY_TEMPLATE['layout'],
                    height=450,
                    xaxis_title='Region',
                    yaxis_title='Number of Vacancies',
                    barmode='group',
                )
                st.plotly_chart(fig, width='stretch')

    # === UK REGION HEATMAP ===
    if region_df is not None and 'uk_region' in region_df.columns:
        geojson_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'uk_regions.geojson')
        if os.path.exists(geojson_path):
            st.markdown(branded_divider(), unsafe_allow_html=True)
            st.markdown(section_header("UK Region Heatmap", "map"), unsafe_allow_html=True)

            filtered_region = apply_filters_to_region_data(region_df, st.session_state.get('global_filters'))

            # Aggregate metrics per region
            map_data = []
            for rgn in filtered_region['uk_region'].dropna().unique():
                if rgn in ('Unknown', 'Overseas Territory'):
                    continue
                rgn_df = filtered_region[filtered_region['uk_region'] == rgn]
                vacancies = rgn_df['entity_id'].nunique() if 'entity_id' in rgn_df.columns else len(rgn_df)
                clients = rgn_df['organization_name'].nunique() if 'organization_name' in rgn_df.columns else 0

                vac_2025 = 0
                vac_2026 = 0
                if 'start_date' in rgn_df.columns:
                    sd = rgn_df['start_date']
                    if pd.api.types.is_datetime64_any_dtype(sd):
                        vac_2025 = rgn_df[sd.dt.year == 2025]['entity_id'].nunique() if 'entity_id' in rgn_df.columns else 0
                        vac_2026 = rgn_df[sd.dt.year == 2026]['entity_id'].nunique() if 'entity_id' in rgn_df.columns else 0

                total_clicks = int(rgn_df['clicks'].sum()) if 'clicks' in rgn_df.columns else 0
                total_applies = int(rgn_df['applies'].sum()) if 'applies' in rgn_df.columns else 0

                map_data.append({
                    'region': rgn,
                    'Vacancies': vacancies,
                    'Clients': clients,
                    'Vacancies (2025)': vac_2025,
                    'Vacancies (2026)': vac_2026,
                    'Total Clicks': total_clicks,
                    'Total Applies': total_applies,
                })

            if map_data:
                map_df = pd.DataFrame(map_data)

                with open(geojson_path) as f:
                    uk_geojson = json.load(f)

                fig_map = px.choropleth(
                    map_df,
                    geojson=uk_geojson,
                    locations='region',
                    featureidkey='properties.region',
                    color='Vacancies',
                    color_continuous_scale=JGP_HEATMAP_COLORSCALE,
                    hover_name='region',
                    hover_data={
                        'region': False,
                        'Vacancies': ':,',
                        'Clients': ':,',
                        'Vacancies (2025)': ':,',
                        'Vacancies (2026)': ':,',
                        'Total Clicks': ':,',
                        'Total Applies': ':,',
                    },
                )
                fig_map.update_geos(
                    fitbounds='locations',
                    visible=False,
                )
                # Use only geo-compatible layout keys (no xaxis/yaxis)
                geo_layout = {k: v for k, v in JGP_PLOTLY_TEMPLATE['layout'].items()
                              if k not in ('xaxis', 'yaxis', 'margin')}
                fig_map.update_layout(
                    **geo_layout,
                    height=650,
                    margin=dict(t=20, b=20, l=20, r=20),
                    coloraxis_colorbar=dict(
                        title='Vacancies',
                        thickness=15,
                    ),
                )
                st.plotly_chart(fig_map, width='stretch')

