"""Salary Benchmarking page - salary intelligence for sales team."""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime
from data.filters import apply_filters_to_data, apply_filters_to_region_data
from data.processing import calculate_salary_statistics, calculate_percentile_rank
from theme.components import (
    kpi_card, page_header, filter_tags, section_header,
    branded_divider, notice_box, empty_state,
)
from theme.colors import JGP_COLORS, JGP_PLOTLY_TEMPLATE, JGP_HEATMAP_COLORSCALE


def _fmt_salary(val):
    """Format salary as £XX,XXX."""
    if val is None or (isinstance(val, float) and np.isnan(val)) or val == 0:
        return "N/A"
    return f"\u00a3{int(round(val)):,}"


def _fmt(val):
    """Format number with thousands separator."""
    return f"{int(round(val)):,}"


def _competitiveness_color(percentile):
    """Return color based on salary competitiveness percentile."""
    if percentile >= 60:
        return JGP_COLORS['deep_green']
    elif percentile >= 40:
        return JGP_COLORS['blue']
    else:
        return JGP_COLORS['negative']


def _competitiveness_label(percentile):
    """Return label based on salary competitiveness percentile."""
    if percentile >= 60:
        return "Competitive"
    elif percentile >= 40:
        return "Market Rate"
    else:
        return "Below Market"


def render_salary(df, region_df=None):
    """Render the Salary Benchmarking page."""

    # Show active filter tags
    if st.session_state.get('global_filters'):
        st.markdown(filter_tags(st.session_state.global_filters), unsafe_allow_html=True)

    st.markdown(page_header("Salary Benchmarking", "Salary intelligence across all live vacancies"), unsafe_allow_html=True)

    # Apply global filters
    filtered_df = apply_filters_to_data(df, st.session_state.get('global_filters'))

    if len(filtered_df) == 0:
        st.markdown(empty_state("No data found for the selected filters.", "funnel"), unsafe_allow_html=True)
        return

    # Check if salary data exists
    if 'has_salary_data' not in filtered_df.columns:
        st.markdown(notice_box("Salary data is not yet available. Please run the BigQuery aggregation update.", "exclamation-triangle"), unsafe_allow_html=True)
        return

    # Data quality banner
    total = len(filtered_df)
    with_salary = filtered_df['has_salary_data'].sum()
    coverage_pct = round(with_salary / total * 100) if total > 0 else 0
    source_counts = filtered_df['salary_source'].value_counts()
    numeric_count = source_counts.get('numeric', 0)
    freetext_count = source_counts.get('free_text', 0)

    st.markdown(notice_box(
        f"{with_salary:,} of {total:,} vacancies have salary data ({coverage_pct}%) "
        f"&mdash; {numeric_count:,} from structured fields, {freetext_count:,} parsed from free text",
        "info-circle"
    ), unsafe_allow_html=True)

    # Salary-specific inline controls
    col_toggle, col_sample = st.columns([2, 1])
    with col_toggle:
        salary_only = st.toggle("Only vacancies with salary data", value=True, key='salary_only_toggle')
    with col_sample:
        min_sample = st.slider("Min sample size for groupings", 3, 20, 5, key='salary_min_sample')

    if salary_only:
        salary_df = filtered_df[filtered_df['has_salary_data']].copy()
    else:
        salary_df = filtered_df.copy()

    if len(salary_df) == 0 or salary_df['has_salary_data'].sum() == 0:
        st.markdown(empty_state("No salary data available for the selected filters. Try broader filters or turn off the salary-only toggle.", "currency-pound"), unsafe_allow_html=True)
        return

    salary_with_data = salary_df[salary_df['has_salary_data']]

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # ================================================================
    # SECTION 1: SALARY COMPETITIVENESS CHECKER
    # ================================================================
    st.markdown(section_header("Salary Competitiveness Checker", "bullseye"), unsafe_allow_html=True)
    st.caption("Enter a salary to see how it compares to the market for a given occupation and region.")

    occupations = sorted(salary_with_data['occupation'].dropna().unique())
    occupations = [o for o in occupations if o != 'Unknown']

    if region_df is not None and 'uk_region' in region_df.columns:
        filtered_region = apply_filters_to_region_data(region_df, st.session_state.get('global_filters'))
        salary_region = filtered_region[filtered_region.get('has_salary_data', True)] if 'has_salary_data' in filtered_region.columns else filtered_region
        regions = sorted(salary_region['uk_region'].dropna().unique())
    else:
        regions = set()
        if 'uk_regions' in salary_with_data.columns:
            for regions_str in salary_with_data['uk_regions'].dropna():
                for r in str(regions_str).split(' | '):
                    r = r.strip()
                    if r:
                        regions.add(r)
        regions = sorted(regions)

    with st.form(key='salary_checker_form'):
        col_salary, col_occ, col_reg = st.columns(3)
        with col_salary:
            input_salary = st.number_input(
                "Salary (annual equivalent)",
                min_value=0, max_value=500000, value=35000, step=1000,
                key='salary_checker_input'
            )
        with col_occ:
            selected_occ = st.selectbox(
                "Occupation",
                ["All Occupations"] + occupations,
                key='salary_checker_occ'
            )
        with col_reg:
            selected_reg = st.selectbox(
                "Region (optional)",
                ["All Regions"] + regions,
                key='salary_checker_reg'
            )
        st.form_submit_button("Check Salary", type="primary")

    # Filter comparison set
    comparison = salary_with_data.copy()
    if selected_occ != "All Occupations":
        comparison = comparison[comparison['occupation'] == selected_occ]
    if selected_reg != "All Regions":
        if 'uk_region' in comparison.columns:
            comparison = comparison[comparison['uk_region'] == selected_reg]
        elif 'uk_regions' in comparison.columns:
            comparison = comparison[comparison['uk_regions'].apply(
                lambda x: selected_reg in [r.strip() for r in str(x).split(' | ')] if pd.notna(x) else False
            )]

    mid_series = comparison['annual_mid_salary'].dropna()

    if len(mid_series) >= 3:
        pct = calculate_percentile_rank(input_salary, mid_series)
        color = _competitiveness_color(pct)
        label = _competitiveness_label(pct)
        stats = calculate_salary_statistics(mid_series)

        occ_label = selected_occ if selected_occ != "All Occupations" else "all occupations"
        reg_label = f" in {selected_reg}" if selected_reg != "All Regions" else ""

        # Result callout
        st.markdown(
            f'<div style="background:linear-gradient(135deg, {color}12, {color}08); '
            f'border-left:4px solid {color}; padding:16px 20px; border-radius:4px; margin:12px 0;">'
            f'<div style="font-family:DM Sans,sans-serif;font-size:22px;font-weight:700;color:{color};">'
            f'{_fmt_salary(input_salary)} for {occ_label}{reg_label}</div>'
            f'<div style="font-family:DM Sans,sans-serif;font-size:16px;color:{color};margin-top:4px;">'
            f'{label} &mdash; higher than <strong>{pct:.0f}%</strong> of advertised salaries '
            f'({stats["count"]:,} vacancies analysed)</div></div>',
            unsafe_allow_html=True
        )

        col_chart, col_stats = st.columns([3, 1])

        with col_chart:
            # P00-P95 viewport clipping: show min up to 95th percentile, drop
            # the top 5% of outliers from the bars (and the bin computation).
            # Means + the user's input salary are computed on the full series
            # and stay where they are — the visible range widens via vline-
            # safety if any of them falls outside the percentile window.
            p95 = mid_series.quantile(0.95)
            clipped = mid_series[mid_series <= p95]

            fig = go.Figure()
            fig.add_trace(go.Histogram(
                x=clipped,
                nbinsx=30,
                marker_color=JGP_COLORS['primary'],
                opacity=0.85,
                name='Salary Distribution',
            ))

            # Vertical lines - same solid style, 3 contrasting colours
            your_color = JGP_COLORS['negative']    # Red
            median_color = JGP_COLORS['blue']      # Blue
            mean_color = JGP_COLORS['deep_green']  # Green

            fig.add_vline(x=input_salary, line_width=3, line_color=your_color)
            fig.add_vline(x=stats['median'], line_width=2, line_color=median_color)
            fig.add_vline(x=stats['mean'], line_width=2, line_color=mean_color)

            references = [input_salary, stats['median'], stats['mean']]
            lo = min(mid_series.min(), *references)
            hi = max(p95, *references)
            pad = (hi - lo) * 0.05 if hi > lo else max(hi * 0.05, 1)
            fig.update_xaxes(range=[lo - pad, hi + pad])

            # Legend entries (invisible traces for the vlines)
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode='lines',
                line=dict(color=your_color, width=3),
                name=f"Your salary: {_fmt_salary(input_salary)}",
            ))
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode='lines',
                line=dict(color=median_color, width=2),
                name=f"Median: {_fmt_salary(stats['median'])}",
            ))
            fig.add_trace(go.Scatter(
                x=[None], y=[None], mode='lines',
                line=dict(color=mean_color, width=2),
                name=f"Mean: {_fmt_salary(stats['mean'])}",
            ))

            layout_overrides = {**JGP_PLOTLY_TEMPLATE['layout']}
            layout_overrides['legend'] = dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                font=dict(size=11),
            )
            fig.update_layout(
                **layout_overrides,
                xaxis_title="Annual Salary",
                yaxis_title="Number of Vacancies",
                showlegend=True,
                height=380,
            )
            st.plotly_chart(fig, use_container_width=True)

        with col_stats:
            stats_min = calculate_salary_statistics(comparison['annual_min_salary'].dropna())
            stats_max = calculate_salary_statistics(comparison['annual_max_salary'].dropna())

            stats_table = pd.DataFrame({
                'Statistic': ['Median', 'Mean', '25th pctl', '75th pctl', 'Range'],
                'Min Salary': [
                    _fmt_salary(stats_min['median']),
                    _fmt_salary(stats_min['mean']),
                    _fmt_salary(stats_min['p25']),
                    _fmt_salary(stats_min['p75']),
                    f"{_fmt_salary(stats_min['min'])} - {_fmt_salary(stats_min['max'])}",
                ],
                'Max Salary': [
                    _fmt_salary(stats_max['median']),
                    _fmt_salary(stats_max['mean']),
                    _fmt_salary(stats_max['p25']),
                    _fmt_salary(stats_max['p75']),
                    f"{_fmt_salary(stats_max['min'])} - {_fmt_salary(stats_max['max'])}",
                ],
            })
            st.dataframe(stats_table, hide_index=True, use_container_width=True)
    else:
        sample_label = f"{selected_occ}{' in ' + selected_reg if selected_reg != 'All Regions' else ''}"
        st.markdown(notice_box(
            f"Not enough salary data for {sample_label} (need at least 3, found {len(mid_series)}). "
            "Try a broader selection.",
            "exclamation-triangle"
        ), unsafe_allow_html=True)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # ================================================================
    # SECTION 2: MARKET SUMMARY STATS
    # ================================================================
    st.markdown(section_header("Market Summary", "currency-pound"), unsafe_allow_html=True)

    overall_mid = calculate_salary_statistics(salary_with_data['annual_mid_salary'])
    overall_min = calculate_salary_statistics(salary_with_data['annual_min_salary'])
    overall_max = calculate_salary_statistics(salary_with_data['annual_max_salary'])

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown(kpi_card("Vacancies with Salary", _fmt(overall_mid['count'])), unsafe_allow_html=True)
    with col2:
        st.markdown(kpi_card("Median Annual Salary", _fmt_salary(overall_mid['median'])), unsafe_allow_html=True)
    with col3:
        st.markdown(kpi_card("Mean Annual Salary", _fmt_salary(overall_mid['mean'])), unsafe_allow_html=True)

    # Detailed stats row
    col_a, col_b, col_c, col_d, col_e, col_f = st.columns(6)
    with col_a:
        st.metric("25th pctl (min)", _fmt_salary(overall_min['p25']))
    with col_b:
        st.metric("25th pctl (max)", _fmt_salary(overall_max['p25']))
    with col_c:
        st.metric("Median (min)", _fmt_salary(overall_min['median']))
    with col_d:
        st.metric("Median (max)", _fmt_salary(overall_max['median']))
    with col_e:
        st.metric("75th pctl (min)", _fmt_salary(overall_min['p75']))
    with col_f:
        st.metric("75th pctl (max)", _fmt_salary(overall_max['p75']))

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # ================================================================
    # SECTION 3: SALARY BY OCCUPATION
    # ================================================================
    st.markdown(section_header("Salary by Occupation", "briefcase"), unsafe_allow_html=True)

    occ_tab1, occ_tab2 = st.tabs(["Box Plot", "Table"])

    # Pre-compute occupation stats
    occ_stats = []
    for occ in salary_with_data['occupation'].unique():
        occ_data = salary_with_data[salary_with_data['occupation'] == occ]
        mid_vals = occ_data['annual_mid_salary'].dropna()
        if len(mid_vals) >= min_sample:
            occ_min_stats = calculate_salary_statistics(occ_data['annual_min_salary'])
            occ_max_stats = calculate_salary_statistics(occ_data['annual_max_salary'])
            occ_stats.append({
                'Occupation': occ,
                'Count': len(mid_vals),
                'Median Min': _fmt_salary(occ_min_stats['median']),
                'Median Max': _fmt_salary(occ_max_stats['median']),
                'Mean Min': _fmt_salary(occ_min_stats['mean']),
                'Mean Max': _fmt_salary(occ_max_stats['mean']),
                '25th pctl': _fmt_salary(occ_min_stats['p25']),
                '75th pctl': _fmt_salary(occ_max_stats['p75']),
                '_median_sort': occ_min_stats['median'],
            })

    with occ_tab1:
        if occ_stats:
            with st.expander("How to read a box plot", icon="\u2139\ufe0f"):
                # Annotated example box plot as a Plotly figure
                guide_fig = go.Figure()
                guide_fig.add_trace(go.Box(
                    x=[25000, 28000, 30000, 32000, 35000, 36000, 38000, 40000, 42000, 45000, 65000],
                    name="Example",
                    marker_color=JGP_COLORS['primary'],
                    line_color=JGP_COLORS['deep_blue'],
                    boxmean=True,
                ))
                guide_fig.add_annotation(x=35000, y=-0.35, text="<b>Median</b><br>Midpoint salary",
                                         showarrow=True, ay=40, font=dict(size=11, color=JGP_COLORS['deep_blue']),
                                         arrowcolor=JGP_COLORS['deep_blue'])
                guide_fig.add_annotation(x=30000, y=0.35, text="<b>25th percentile</b><br>Bottom of box",
                                         showarrow=True, ay=-40, font=dict(size=11, color=JGP_COLORS['primary']),
                                         arrowcolor=JGP_COLORS['primary'])
                guide_fig.add_annotation(x=42000, y=0.35, text="<b>75th percentile</b><br>Top of box",
                                         showarrow=True, ay=-40, font=dict(size=11, color=JGP_COLORS['primary']),
                                         arrowcolor=JGP_COLORS['primary'])
                guide_fig.add_annotation(x=25000, y=-0.35, text="<b>Whisker</b><br>Min typical value",
                                         showarrow=True, ay=40, font=dict(size=11, color=JGP_COLORS['supporting']),
                                         arrowcolor=JGP_COLORS['supporting'])
                guide_fig.add_annotation(x=65000, y=-0.35, text="<b>Outlier</b><br>Unusually high",
                                         showarrow=True, ay=40, font=dict(size=11, color=JGP_COLORS['supporting']),
                                         arrowcolor=JGP_COLORS['supporting'])
                guide_fig.update_layout(
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font=dict(family="DM Sans, sans-serif", size=12),
                    height=180, margin=dict(t=10, b=60, l=10, r=10),
                    xaxis=dict(title="Annual Salary", tickformat=",", gridcolor=JGP_COLORS['light_purple']),
                    yaxis=dict(visible=False),
                    showlegend=False,
                )
                st.plotly_chart(guide_fig, use_container_width=True)
                st.caption("The **box** shows the middle 50% of salaries. The **dashed line** inside is the mean (average). Dots beyond the whiskers are outliers.")

            # Top 20 occupations by count for box plot
            top_occs = sorted(occ_stats, key=lambda x: x['Count'], reverse=True)[:20]
            top_occ_names = [o['Occupation'] for o in top_occs]
            box_data = salary_with_data[salary_with_data['occupation'].isin(top_occ_names)]

            fig = go.Figure()
            # Sort by median salary for visual clarity
            occ_order = (box_data.groupby('occupation')['annual_mid_salary']
                         .median().sort_values(ascending=True).index.tolist())
            for occ in occ_order:
                occ_vals = box_data[box_data['occupation'] == occ]['annual_mid_salary'].dropna()
                fig.add_trace(go.Box(
                    x=occ_vals,
                    name=occ,
                    marker_color=JGP_COLORS['primary'],
                    line_color=JGP_COLORS['deep_blue'],
                    boxmean=True,
                ))
            fig.update_layout(
                **JGP_PLOTLY_TEMPLATE['layout'],
                xaxis_title="Annual Salary",
                showlegend=False,
                height=min(1200, max(400, len(occ_order) * 35)),
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.markdown(empty_state(f"No occupations with at least {min_sample} salary data points.", "briefcase"), unsafe_allow_html=True)

    with occ_tab2:
        if occ_stats:
            occ_df = pd.DataFrame(occ_stats).sort_values('Count', ascending=False).drop(columns=['_median_sort'])
            st.dataframe(occ_df, hide_index=True, use_container_width=True, height=500)
            csv = occ_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                "Download Salary by Occupation",
                csv,
                f"salary_by_occupation_{datetime.now().strftime('%Y%m%d')}.csv",
                "text/csv",
                key='dl_occ_salary',
            )
        else:
            st.markdown(empty_state(f"No occupations with at least {min_sample} salary data points.", "briefcase"), unsafe_allow_html=True)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # ================================================================
    # SECTION 4: SALARY BY REGION
    # ================================================================
    st.markdown(section_header("Salary by Region", "geo-alt"), unsafe_allow_html=True)

    reg_tab1, reg_tab2 = st.tabs(["Heatmap", "Table"])

    # Pre-compute region stats
    reg_stats = []
    reg_median_map = {}

    if region_df is not None and 'uk_region' in region_df.columns:
        # Use pre-exploded region table
        filtered_region = apply_filters_to_region_data(region_df, st.session_state.get('global_filters'))
        if salary_only and 'has_salary_data' in filtered_region.columns:
            salary_region = filtered_region[filtered_region['has_salary_data']]
        else:
            salary_region = filtered_region
        salary_region_with_data = salary_region[salary_region.get('has_salary_data', True)] if 'has_salary_data' in salary_region.columns else salary_region

        for region in sorted(salary_region_with_data['uk_region'].dropna().unique()):
            reg_data = salary_region_with_data[salary_region_with_data['uk_region'] == region]
            mid_vals = reg_data['annual_mid_salary'].dropna()
            if len(mid_vals) >= min_sample:
                reg_min_stats = calculate_salary_statistics(reg_data['annual_min_salary'])
                reg_max_stats = calculate_salary_statistics(reg_data['annual_max_salary'])
                mid_stats = calculate_salary_statistics(mid_vals)
                reg_median_map[region] = mid_stats['median']
                reg_stats.append({
                    'Region': region,
                    'Count': len(mid_vals),
                    'Median Min': _fmt_salary(reg_min_stats['median']),
                    'Median Max': _fmt_salary(reg_max_stats['median']),
                    'Mean Min': _fmt_salary(reg_min_stats['mean']),
                    'Mean Max': _fmt_salary(reg_max_stats['mean']),
                    '25th pctl': _fmt_salary(reg_min_stats['p25']),
                    '75th pctl': _fmt_salary(reg_max_stats['p75']),
                    '_median_sort': mid_stats['median'],
                })
    else:
        # Fallback: pipe-split from vacancy summary
        all_regions = set()
        if 'uk_regions' in salary_with_data.columns:
            for regions_str in salary_with_data['uk_regions'].dropna():
                for r in str(regions_str).split(' | '):
                    r = r.strip()
                    if r:
                        all_regions.add(r)

        for region in all_regions:
            mask = salary_with_data['uk_regions'].apply(
                lambda x, r=region: r in [rr.strip() for rr in str(x).split(' | ')] if pd.notna(x) else False
            )
            reg_data = salary_with_data[mask]
            mid_vals = reg_data['annual_mid_salary'].dropna()
            if len(mid_vals) >= min_sample:
                reg_min_stats = calculate_salary_statistics(reg_data['annual_min_salary'])
                reg_max_stats = calculate_salary_statistics(reg_data['annual_max_salary'])
                mid_stats = calculate_salary_statistics(mid_vals)
                reg_median_map[region] = mid_stats['median']
                reg_stats.append({
                    'Region': region,
                    'Count': len(mid_vals),
                    'Median Min': _fmt_salary(reg_min_stats['median']),
                    'Median Max': _fmt_salary(reg_max_stats['median']),
                    'Mean Min': _fmt_salary(reg_min_stats['mean']),
                    'Mean Max': _fmt_salary(reg_max_stats['mean']),
                    '25th pctl': _fmt_salary(reg_min_stats['p25']),
                    '75th pctl': _fmt_salary(reg_max_stats['p75']),
                    '_median_sort': mid_stats['median'],
                })

    with reg_tab1:
        if reg_median_map:
            hm_df = pd.DataFrame([
                {'Region': r, 'Median Salary': v}
                for r, v in sorted(reg_median_map.items(), key=lambda x: x[1], reverse=True)
            ])
            fig = px.bar(
                hm_df,
                y='Region',
                x='Median Salary',
                orientation='h',
                color='Median Salary',
                color_continuous_scale=JGP_HEATMAP_COLORSCALE,
                text=hm_df['Median Salary'].apply(lambda v: _fmt_salary(v)),
            )
            fig.update_layout(
                **JGP_PLOTLY_TEMPLATE['layout'],
                xaxis_title="Median Annual Salary",
                yaxis_title="",
                showlegend=False,
                height=min(1200, max(400, len(hm_df) * 35)),
                coloraxis_showscale=False,
            )
            fig.update_traces(textposition='outside')
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.markdown(empty_state(f"No regions with at least {min_sample} salary data points.", "geo-alt"), unsafe_allow_html=True)

    with reg_tab2:
        if reg_stats:
            reg_df = pd.DataFrame(reg_stats).sort_values('_median_sort', ascending=False).drop(columns=['_median_sort'])
            st.dataframe(reg_df, hide_index=True, use_container_width=True, height=500)
            csv = reg_df.to_csv(index=False).encode('utf-8')
            st.download_button(
                "Download Salary by Region",
                csv,
                f"salary_by_region_{datetime.now().strftime('%Y%m%d')}.csv",
                "text/csv",
                key='dl_reg_salary',
            )
        else:
            st.markdown(empty_state(f"No regions with at least {min_sample} salary data points.", "geo-alt"), unsafe_allow_html=True)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # ================================================================
    # SECTION 5: SALARY BANDS
    # ================================================================
    st.markdown(section_header("Salary Bands", "bar-chart-steps"), unsafe_allow_html=True)

    band_edges = [0, 20000, 30000, 40000, 50000, 60000, 80000, 100000, float('inf')]
    band_labels = ['<\u00a320k', '\u00a320-30k', '\u00a330-40k', '\u00a340-50k',
                   '\u00a350-60k', '\u00a360-80k', '\u00a380-100k', '\u00a3100k+']
    band_colors = [
        JGP_COLORS['pink'], JGP_COLORS['light_purple'], JGP_COLORS['blue'],
        JGP_COLORS['light_green'], JGP_COLORS['supporting'], JGP_COLORS['primary'],
        JGP_COLORS['deep_blue'], JGP_COLORS['deep_green'],
    ]

    mid_with_data = salary_with_data['annual_mid_salary'].dropna()
    if len(mid_with_data) > 0:
        salary_with_data = salary_with_data.copy()
        salary_with_data['salary_band'] = pd.cut(
            salary_with_data['annual_mid_salary'],
            bins=band_edges,
            labels=band_labels,
            right=False,
        )

        col_donut, col_stacked = st.columns([1, 2])

        with col_donut:
            band_counts = salary_with_data['salary_band'].value_counts().reindex(band_labels).fillna(0)
            fig = go.Figure(go.Pie(
                labels=band_counts.index,
                values=band_counts.values,
                hole=0.5,
                marker_colors=band_colors,
                textinfo='label+percent',
                textposition='outside',
            ))
            fig.update_layout(
                **JGP_PLOTLY_TEMPLATE['layout'],
                showlegend=False,
                height=400,
            )
            st.plotly_chart(fig, use_container_width=True)

        with col_stacked:
            # Stacked bar by top occupations
            top_occs_for_bands = (salary_with_data[salary_with_data['has_salary_data']]
                                  .groupby('occupation').size()
                                  .nlargest(15).index.tolist())
            band_occ_data = salary_with_data[
                salary_with_data['occupation'].isin(top_occs_for_bands) &
                salary_with_data['salary_band'].notna()
            ]

            if len(band_occ_data) > 0:
                pivot = band_occ_data.groupby(['occupation', 'salary_band'], observed=True).size().unstack(fill_value=0)
                # Reorder columns to match band order
                pivot = pivot.reindex(columns=[b for b in band_labels if b in pivot.columns], fill_value=0)
                # Sort by total vacancies
                pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=True).index]

                fig = go.Figure()
                for i, band in enumerate(pivot.columns):
                    fig.add_trace(go.Bar(
                        y=pivot.index,
                        x=pivot[band],
                        name=band,
                        orientation='h',
                        marker_color=band_colors[band_labels.index(band)],
                    ))
                # Merge template layout with overrides (legend already in template)
                layout_args = {**JGP_PLOTLY_TEMPLATE['layout']}
                layout_args['legend'] = dict(
                    orientation="h",
                    yanchor="bottom",
                    y=1.02,
                    xanchor="left",
                    x=0,
                    bgcolor="rgba(255,255,255,0.8)",
                    bordercolor=JGP_COLORS['light_purple'],
                    borderwidth=1,
                    font=dict(size=11),
                )
                fig.update_layout(
                    **layout_args,
                    barmode='stack',
                    xaxis_title="Number of Vacancies",
                    yaxis_title="",
                    height=min(1200, max(400, len(pivot) * 30)),
                )
                st.plotly_chart(fig, use_container_width=True)
    else:
        st.markdown(empty_state("No salary data available for band analysis.", "bar-chart-steps"), unsafe_allow_html=True)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # ================================================================
    # SECTION 6: VACANCY DETAILS
    # ================================================================
    st.markdown(section_header("Vacancy Details", "table"), unsafe_allow_html=True)

    vacancy_data = []
    display_df = salary_with_data if salary_only else filtered_df[filtered_df['has_salary_data']]

    for _, row in display_df.iterrows():
        # vacancy_status (Published/Unpublished) replaces frozen workflow_state;
        # fall back to workflow_state if not present yet (deploy→refresh window).
        _vs = row.get('vacancy_status')
        vac_status = _vs if (_vs is not None and pd.notna(_vs)) else row.get('workflow_state', '')
        vacancy_data.append({
            'Title': row.get('title', 'Unknown'),
            'Organisation': row.get('organization_name', 'Unknown'),
            'Occupation': row.get('occupation', 'Unknown'),
            'Region': row.get('uk_regions', ''),
            'Annual Min': _fmt_salary(row.get('annual_min_salary')),
            'Annual Max': _fmt_salary(row.get('annual_max_salary')),
            'Currency': row.get('currency_code', ''),
            'Source': row.get('salary_source', ''),
            'Status': vac_status,
            'Type': row.get('employment_type', ''),
            '_sort_salary': row.get('annual_mid_salary', 0) if pd.notna(row.get('annual_mid_salary')) else 0,
        })

    if vacancy_data:
        vac_df = pd.DataFrame(vacancy_data)
        vac_df = vac_df.sort_values('_sort_salary', ascending=False).drop(columns=['_sort_salary'])

        st.caption(f"Showing {len(vac_df):,} vacancies with salary data")
        st.dataframe(vac_df, hide_index=True, use_container_width=True, height=600)

        csv = vac_df.to_csv(index=False).encode('utf-8')
        st.download_button(
            "Download Vacancy Salary Data",
            csv,
            f"salary_vacancies_{datetime.now().strftime('%Y%m%d')}.csv",
            "text/csv",
            key='dl_vac_salary',
        )
    else:
        st.markdown(empty_state("No vacancies with salary data found.", "table"), unsafe_allow_html=True)
