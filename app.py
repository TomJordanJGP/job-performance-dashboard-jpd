"""JGP Job Performance Dashboard - Branded UI version."""

import streamlit as st
from datetime import datetime

from theme.colors import JGP_LOGOS

# Page configuration (must be first Streamlit command)
st.set_page_config(
    page_title="JGP Job Performance Dashboard",
    page_icon=JGP_LOGOS['favicon'],
    layout="wide",
    initial_sidebar_state="expanded"
)

# Inject branded CSS
from theme.css import inject_css
inject_css()

# Import modules
from theme.components import sidebar_logo, main_logo, sidebar_section_header
from data.loader import load_all_data
from data.processing import (
    prepare_enriched_data,
    apply_importer_mapping,
    parse_upgrades,
    parse_dates_in_jobiqo,
    add_occupation_column,
    process_salary_columns,
)
from data.filters import create_sidebar_filters, apply_filters_to_region_data
from views.dashboard import render_dashboard
from views.performance import render_performance
from views.compare import render_compare
from views.salary import render_salary
from views.client_report import render_client_report


@st.cache_data(ttl=14400)
def _process_raw_data(df_raw):
    """Apply all enrichment steps to raw data (cached to avoid reprocessing on reruns)."""
    df = df_raw.copy()
    df = prepare_enriched_data(df)
    df = apply_importer_mapping(df)
    df = parse_upgrades(df)
    df = parse_dates_in_jobiqo(df)
    df = add_occupation_column(df)
    df = process_salary_columns(df)
    return df


def main():
    # === SIDEBAR ===
    with st.sidebar:
        # JGP Logo
        st.markdown(sidebar_logo(), unsafe_allow_html=True)

    # === DATA LOADING (all data, no date cutoff) ===
    with st.spinner("Loading data..."):
        # Defensive unpack: tolerates loader returning 3 or 4 values so a
        # stale @st.cache_data hit on Streamlit Cloud during the rollout
        # doesn't crash the app. Drop once we've confirmed clean redeploy.
        loaded = load_all_data(sample_size=None)
        df_raw, daily_totals, region_raw = loaded[:3]
        media_df = loaded[3] if len(loaded) >= 4 else None
        df = _process_raw_data(df_raw)
        region_df = _process_raw_data(region_raw) if region_raw is not None else None

    # Initialize session state
    for key in ['global_filters', 'comp_left_filters', 'comp_right_filters']:
        if key not in st.session_state:
            st.session_state[key] = None

    # === SIDEBAR FILTERS ===
    with st.sidebar:
        st.markdown(sidebar_section_header("Filters"), unsafe_allow_html=True)
        filters, apply_clicked = create_sidebar_filters(df, region_df=region_df)

        # Apply filters to session state
        if apply_clicked:
            st.session_state.global_filters = filters

        # Stats glossary
        st.markdown("---")
        with st.expander("Understanding the stats", icon="\u2139\ufe0f"):
            st.markdown(
                "**Median** \u2014 The middle value when all salaries are "
                "sorted. Half are above, half below. Less affected by "
                "extreme outliers than the mean.\n\n"
                "**Mean (Average)** \u2014 The total of all salaries divided "
                "by the number of vacancies. Can be pulled up by a few "
                "very high salaries.\n\n"
                "**Percentile** \u2014 Shows where a value sits relative to "
                "the rest. The 75th percentile means 75% of salaries "
                "are below that figure."
            )

        # Footer
        st.caption(f"Last refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        st.caption(f"Total vacancies: {len(df):,}")
        if st.button("Refresh Data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    # === MAIN CONTENT ===
    # Logo above tabs
    st.markdown(main_logo(), unsafe_allow_html=True)

    # Tabs
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Dashboard",
        "Performance",
        "Compare",
        "Salary Benchmarking",
        "Client Report",
    ])

    with tab1:
        render_dashboard(df, daily_totals=daily_totals, region_df=region_df)

    with tab2:
        render_performance(df, region_df=region_df)

    with tab3:
        render_compare(df)

    with tab4:
        render_salary(df, region_df=region_df)

    with tab5:
        render_client_report(df, media_df=media_df)


if __name__ == "__main__":
    main()
