"""Filter creation and application functions."""

import streamlit as st
import pandas as pd
from datetime import datetime
from data.regions import (
    get_available_countries,
    get_regions_for_countries,
    resolve_country_region_selections,
)


def _get_available_regions(df, region_df=None):
    """Extract all unique regions.

    Uses region_df (one row per vacancy per region) when available,
    otherwise falls back to pipe-splitting uk_regions column.
    """
    if region_df is not None and 'uk_region' in region_df.columns:
        return set(region_df['uk_region'].dropna().unique())
    all_regions = set()
    if 'uk_regions' in df.columns:
        for regions_str in df['uk_regions'].dropna():
            for r in str(regions_str).split(' | '):
                r = r.strip()
                if r:
                    all_regions.add(r)
    return all_regions


def create_sidebar_filters(df, key_prefix='global', region_df=None):
    """Create the global filter panel in the sidebar.

    Widgets are live (dynamic) so selecting Country updates Region options.
    Filters only apply to the dashboard when 'Apply Filters' is clicked.

    Returns:
        tuple: (filters_dict, apply_clicked_bool)
    """
    filters = {}

    # Date Range
    date_col = 'last_event_date' if 'last_event_date' in df.columns else None
    if date_col and pd.api.types.is_datetime64_any_dtype(df[date_col]):
        min_date = df['first_event_date'].dropna().min() if 'first_event_date' in df.columns else df[date_col].dropna().min()
        max_date = df[date_col].dropna().max()
        if pd.isna(min_date) or pd.isna(max_date):
            min_date = datetime.now().date()
            max_date = datetime.now().date()
        else:
            min_date = min_date.date() if hasattr(min_date, 'date') else min_date
            max_date = max_date.date() if hasattr(max_date, 'date') else max_date

        filters['date_range'] = st.sidebar.date_input(
            "Date Range",
            [min_date, max_date],
            min_value=min_date,
            max_value=max_date,
            key=f'{key_prefix}_date'
        )

    # Importer
    if 'importer_name' in df.columns:
        importers = sorted(df['importer_name'].dropna().unique())
        filters['importer'] = st.sidebar.multiselect(
            "Importer",
            importers,
            key=f'{key_prefix}_importer'
        )

    # Company
    if 'organization_name' in df.columns:
        companies = sorted(df['organization_name'].dropna().unique())
        filters['company'] = st.sidebar.multiselect(
            "Client / Company",
            companies,
            key=f'{key_prefix}_company'
        )

    # Country / Region (two linked filters - dynamic)
    available_regions = _get_available_regions(df, region_df=region_df)
    if available_regions:
        country_options = get_available_countries(available_regions)
        selected_countries = st.sidebar.multiselect(
            "Country",
            country_options,
            key=f'{key_prefix}_country'
        )

        region_options = get_regions_for_countries(selected_countries, available_regions)
        selected_regions = st.sidebar.multiselect(
            "Region",
            region_options,
            key=f'{key_prefix}_region'
        )

        filters['region'] = resolve_country_region_selections(
            selected_countries, selected_regions, available_regions
        )

    # Occupation
    if 'occupation' in df.columns:
        occupations = sorted(df['occupation'].dropna().unique())
        filters['occupation'] = st.sidebar.multiselect(
            "Occupation",
            occupations,
            key=f'{key_prefix}_occupation'
        )

    # Job Title Search
    filters['job_title'] = st.sidebar.text_input(
        "Job Title (search)",
        key=f'{key_prefix}_title',
        placeholder="e.g., Housing Director"
    )

    # Upgrades
    if 'upgrades_list' in df.columns:
        all_upgrades = set()
        for upgrades in df['upgrades_list']:
            all_upgrades.update(upgrades)
        upgrade_options = sorted(list(all_upgrades))
        filters['upgrades'] = st.sidebar.multiselect(
            "Upgrades",
            upgrade_options,
            key=f'{key_prefix}_upgrades'
        )

    # Entity ID (vacancy ID)
    if 'entity_id' in df.columns:
        entity_ids = sorted(df['entity_id'].dropna().astype(str).unique())
        filters['entity_id'] = st.sidebar.multiselect(
            "Entity ID",
            entity_ids,
            key=f'{key_prefix}_entity_id',
            placeholder="Search and select vacancy IDs"
        )

    # Apply / Clear buttons
    btn_col1, btn_col2 = st.sidebar.columns(2)
    with btn_col1:
        apply_clicked = st.button(
            "Apply Filters",
            key=f'{key_prefix}_apply',
            type="primary",
            use_container_width=True
        )
    with btn_col2:
        clear_clicked = st.button(
            "Clear All",
            key=f'{key_prefix}_clear',
            use_container_width=True
        )

    if clear_clicked:
        # Clear applied filter state
        st.session_state.pop('global_filters', None)
        st.session_state.pop('comp_left_filters', None)
        st.session_state.pop('comp_right_filters', None)
        # Clear all widget values for this filter set
        widget_keys = [
            f'{key_prefix}_date', f'{key_prefix}_importer',
            f'{key_prefix}_company', f'{key_prefix}_country',
            f'{key_prefix}_region', f'{key_prefix}_occupation',
            f'{key_prefix}_title', f'{key_prefix}_upgrades',
            f'{key_prefix}_entity_id',
        ]
        for key in widget_keys:
            st.session_state.pop(key, None)
        st.rerun()

    return filters, apply_clicked


def create_inline_filters(df, key_prefix):
    """Create a compact inline filter panel (used in Compare page).

    Returns:
        tuple: (filters_dict, apply_clicked_bool)
    """
    filters = {}

    # Date Range
    date_col = 'last_event_date' if 'last_event_date' in df.columns else None
    if date_col and pd.api.types.is_datetime64_any_dtype(df[date_col]):
        min_date = df['first_event_date'].dropna().min() if 'first_event_date' in df.columns else df[date_col].dropna().min()
        max_date = df[date_col].dropna().max()
        if pd.isna(min_date) or pd.isna(max_date):
            min_date = datetime.now().date()
            max_date = datetime.now().date()
        else:
            min_date = min_date.date() if hasattr(min_date, 'date') else min_date
            max_date = max_date.date() if hasattr(max_date, 'date') else max_date

        filters['date_range'] = st.date_input(
            "Date Range",
            [min_date, max_date],
            min_value=min_date,
            max_value=max_date,
            key=f'{key_prefix}_date'
        )

    # Importer
    if 'importer_name' in df.columns:
        importers = sorted(df['importer_name'].dropna().unique())
        filters['importer'] = st.multiselect(
            "Importer",
            importers,
            key=f'{key_prefix}_importer'
        )

    # Company
    if 'organization_name' in df.columns:
        companies = sorted(df['organization_name'].dropna().unique())
        filters['company'] = st.multiselect(
            "Client / Company",
            companies,
            key=f'{key_prefix}_company'
        )

    # Country / Region (two linked filters)
    available_regions = _get_available_regions(df)
    if available_regions:
        country_options = get_available_countries(available_regions)
        selected_countries = st.multiselect(
            "Country",
            country_options,
            key=f'{key_prefix}_country'
        )

        region_options = get_regions_for_countries(selected_countries, available_regions)
        selected_regions = st.multiselect(
            "Region",
            region_options,
            key=f'{key_prefix}_region'
        )

        filters['region'] = resolve_country_region_selections(
            selected_countries, selected_regions, available_regions
        )

    # Occupation
    if 'occupation' in df.columns:
        filters['occupation'] = st.multiselect(
            "Occupation",
            sorted(df['occupation'].dropna().unique()),
            key=f'{key_prefix}_occupation'
        )

    # Upgrades
    if 'upgrades_list' in df.columns:
        all_upgrades = set()
        for upgrades in df['upgrades_list']:
            all_upgrades.update(upgrades)
        filters['upgrades'] = st.multiselect(
            "Upgrades",
            sorted(list(all_upgrades)),
            key=f'{key_prefix}_upgrades'
        )

    # Entity ID (vacancy ID)
    if 'entity_id' in df.columns:
        filters['entity_id'] = st.multiselect(
            "Entity ID",
            sorted(df['entity_id'].dropna().astype(str).unique()),
            key=f'{key_prefix}_entity_id',
            placeholder="Search and select vacancy IDs"
        )

    # Apply button
    apply_clicked = st.button(
        "Apply Filters",
        key=f'{key_prefix}_apply',
        type="primary",
        use_container_width=True
    )

    return filters, apply_clicked


def apply_filters_to_data(df, filters):
    """Apply filter selections to dataframe."""
    if filters is None:
        return df.copy()

    filtered = df.copy()

    # Date Range
    if filters.get('date_range') and len(filters['date_range']) == 2:
        start_date, end_date = filters['date_range']
        if 'first_event_date' in filtered.columns and 'last_event_date' in filtered.columns:
            if pd.api.types.is_datetime64_any_dtype(filtered['last_event_date']):
                filtered = filtered[
                    (filtered['first_event_date'].dt.date <= end_date) &
                    (filtered['last_event_date'].dt.date >= start_date)
                ]

    # Importer
    if filters.get('importer') and 'importer_name' in filtered.columns:
        filtered = filtered[filtered['importer_name'].isin(filters['importer'])]

    # Company
    if filters.get('company') and 'organization_name' in filtered.columns:
        filtered = filtered[filtered['organization_name'].isin(filters['company'])]

    # Region (already resolved from country selections)
    if filters.get('region') and 'uk_regions' in filtered.columns:
        selected_regions = set(filters['region'])
        mask = filtered['uk_regions'].apply(
            lambda x: bool(selected_regions & set(r.strip() for r in str(x).split(' | ')))
            if pd.notna(x) else False
        )
        filtered = filtered[mask]

    # Occupation
    if filters.get('occupation') and 'occupation' in filtered.columns:
        filtered = filtered[filtered['occupation'].isin(filters['occupation'])]

    # Upgrades
    if filters.get('upgrades') and 'upgrades_list' in filtered.columns:
        filtered = filtered[filtered['upgrades_list'].apply(
            lambda x: any(upgrade in x for upgrade in filters['upgrades'])
        )]

    # Job Title Search
    if filters.get('job_title') and filters['job_title'].strip():
        if 'title' in filtered.columns:
            search_term = filters['job_title'].strip().lower()
            filtered = filtered[filtered['title'].str.lower().str.contains(search_term, na=False, regex=False)]

    # Entity ID
    if filters.get('entity_id') and 'entity_id' in filtered.columns:
        selected_ids = set(str(x) for x in filters['entity_id'])
        filtered = filtered[filtered['entity_id'].astype(str).isin(selected_ids)]

    return filtered


def apply_filters_to_region_data(region_df, filters):
    """Apply filter selections to the region-exploded DataFrame.

    Same logic as apply_filters_to_data except the region filter uses
    direct equality on the uk_region column (no pipe-splitting needed).
    """
    if filters is None or region_df is None:
        return region_df.copy() if region_df is not None else pd.DataFrame()

    filtered = region_df.copy()

    # Date Range
    if filters.get('date_range') and len(filters['date_range']) == 2:
        start_date, end_date = filters['date_range']
        if 'first_event_date' in filtered.columns and 'last_event_date' in filtered.columns:
            if pd.api.types.is_datetime64_any_dtype(filtered['last_event_date']):
                filtered = filtered[
                    (filtered['first_event_date'].dt.date <= end_date) &
                    (filtered['last_event_date'].dt.date >= start_date)
                ]

    # Importer
    if filters.get('importer') and 'importer_name' in filtered.columns:
        filtered = filtered[filtered['importer_name'].isin(filters['importer'])]

    # Company
    if filters.get('company') and 'organization_name' in filtered.columns:
        filtered = filtered[filtered['organization_name'].isin(filters['company'])]

    # Region — direct equality, no pipe-split needed
    if filters.get('region') and 'uk_region' in filtered.columns:
        filtered = filtered[filtered['uk_region'].isin(filters['region'])]

    # Occupation
    if filters.get('occupation') and 'occupation' in filtered.columns:
        filtered = filtered[filtered['occupation'].isin(filters['occupation'])]

    # Upgrades
    if filters.get('upgrades') and 'upgrades_list' in filtered.columns:
        filtered = filtered[filtered['upgrades_list'].apply(
            lambda x: any(upgrade in x for upgrade in filters['upgrades'])
        )]

    # Job Title Search
    if filters.get('job_title') and filters['job_title'].strip():
        if 'title' in filtered.columns:
            search_term = filters['job_title'].strip().lower()
            filtered = filtered[filtered['title'].str.lower().str.contains(search_term, na=False, regex=False)]

    # Entity ID
    if filters.get('entity_id') and 'entity_id' in filtered.columns:
        selected_ids = set(str(x) for x in filters['entity_id'])
        filtered = filtered[filtered['entity_id'].astype(str).isin(selected_ids)]

    return filtered
