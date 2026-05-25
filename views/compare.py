"""Compare page - side-by-side segment comparison."""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from data.calculations import calculate_metrics
from data.filters import create_inline_filters, apply_filters_to_data
from theme.components import (
    page_header, section_header,
    branded_divider, notice_box,
)
from theme.colors import JGP_COLORS, JGP_PLOTLY_TEMPLATE


def _fmt(val):
    """Format number: whole number with thousands separator."""
    return f"{int(round(val)):,}"


def render_compare(df):
    """Render the Compare page."""

    st.markdown(page_header("Compare"), unsafe_allow_html=True)
    st.markdown(notice_box("Global filters are not applied on this page. Use the side-by-side filters below.", "info-circle"), unsafe_allow_html=True)

    col_left, col_right = st.columns(2)

    # Side A
    with col_left:
        st.markdown(section_header("Side A", "layout-sidebar"), unsafe_allow_html=True)
        with st.expander("Filters", expanded=True):
            filters_left, apply_left = create_inline_filters(df, 'comp_left')

        if apply_left or st.session_state.get('comp_left_filters'):
            if apply_left:
                st.session_state.comp_left_filters = filters_left
            filtered_left = apply_filters_to_data(df, st.session_state.comp_left_filters)
        else:
            filtered_left = df.copy()

    # Side B
    with col_right:
        st.markdown(section_header("Side B", "layout-sidebar-reverse"), unsafe_allow_html=True)
        with st.expander("Filters", expanded=True):
            filters_right, apply_right = create_inline_filters(df, 'comp_right')

        if apply_right or st.session_state.get('comp_right_filters'):
            if apply_right:
                st.session_state.comp_right_filters = filters_right
            filtered_right = apply_filters_to_data(df, st.session_state.comp_right_filters)
        else:
            filtered_right = df.copy()

    metrics_left = calculate_metrics(filtered_left)
    metrics_right = calculate_metrics(filtered_right)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # === COMPARISON TABLE ===
    st.markdown(section_header("Comparison Summary", "arrow-left-right"), unsafe_allow_html=True)

    # Build comparison data as a table
    comparison_rows = [
        ("Vacancies", metrics_left['num_vacancies'], metrics_right['num_vacancies']),
        ("Clicks", metrics_left['total_clicks'], metrics_right['total_clicks']),
        ("Applies", metrics_left['total_applies'], metrics_right['total_applies']),
        ("Apply/Click %", metrics_left['apply_click_ratio'], metrics_right['apply_click_ratio']),
        ("Clicks/Vacancy", metrics_left['clicks_per_vacancy'], metrics_right['clicks_per_vacancy']),
        ("Applies/Vacancy", metrics_left['applies_per_vacancy'], metrics_right['applies_per_vacancy']),
    ]

    table_data = []
    for label, val_a, val_b in comparison_rows:
        if label == "Apply/Click %":
            a_str = f"{round(val_a)}%"
            b_str = f"{round(val_b)}%"
            diff = val_b - val_a
            pct_str = f"{round(diff):+}pp"
        else:
            a_str = _fmt(val_a)
            b_str = _fmt(val_b)
            diff = val_b - val_a
            if val_a > 0:
                pct_change = ((val_b / val_a) - 1) * 100
                pct_str = f"{round(pct_change):+}%"
            else:
                pct_str = "N/A"

        table_data.append({
            'Metric': label,
            'Side A': a_str,
            'Side B': b_str,
            'Difference': pct_str,
        })

    comparison_df = pd.DataFrame(table_data)
    st.dataframe(comparison_df, width='stretch', hide_index=True)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # === INDEXED COMPARISON CHART ===
    st.markdown(section_header("Comparison Chart (Side A = 100%)", "bar-chart-line"), unsafe_allow_html=True)

    chart_metrics = [
        ("Vacancies", metrics_left['num_vacancies'], metrics_right['num_vacancies']),
        ("Clicks", metrics_left['total_clicks'], metrics_right['total_clicks']),
        ("Applies", metrics_left['total_applies'], metrics_right['total_applies']),
        ("Apply/Click %", metrics_left['apply_click_ratio'], metrics_right['apply_click_ratio']),
        ("Clicks/Vacancy", metrics_left['clicks_per_vacancy'], metrics_right['clicks_per_vacancy']),
        ("Applies/Vacancy", metrics_left['applies_per_vacancy'], metrics_right['applies_per_vacancy']),
    ]

    labels = []
    side_a_pct = []
    side_b_pct = []
    for label, val_a, val_b in chart_metrics:
        labels.append(label)
        side_a_pct.append(100)
        if val_a > 0:
            side_b_pct.append(round((val_b / val_a) * 100))
        else:
            side_b_pct.append(0)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=labels,
        x=side_a_pct,
        name='Side A (baseline)',
        orientation='h',
        marker_color=JGP_COLORS['primary'],
        text=[f"{v}%" for v in side_a_pct],
        textposition='auto',
    ))
    fig.add_trace(go.Bar(
        y=labels,
        x=side_b_pct,
        name='Side B',
        orientation='h',
        marker_color=JGP_COLORS['supporting'],
        text=[f"{v}%" for v in side_b_pct],
        textposition='auto',
    ))
    fig.update_layout(
        **JGP_PLOTLY_TEMPLATE['layout'],
        height=350,
        xaxis_title='Indexed % (Side A = 100%)',
        yaxis_title=None,
        barmode='group',
    )
    st.plotly_chart(fig, width='stretch')
