"""UK country and region mapping for hierarchical filtering."""

# Country -> Regions mapping
COUNTRY_REGIONS = {
    'England': [
        'North East',
        'North West',
        'Yorkshire and The Humber',
        'East Midlands',
        'West Midlands',
        'East of England',
        'Greater London',
        'South East',
        'South West',
    ],
    'Scotland': [
        'Scotland',
    ],
    'Wales': [
        'Wales',
    ],
    'Northern Ireland': [
        'Northern Ireland',
    ],
}

# Reverse lookup: region -> country
REGION_TO_COUNTRY = {}
for country, regions in COUNTRY_REGIONS.items():
    for region in regions:
        REGION_TO_COUNTRY[region] = country


def get_country_for_region(region_name):
    """Return the country for a given region name."""
    return REGION_TO_COUNTRY.get(region_name, 'Unknown')


def get_all_regions_for_country(country_name):
    """Return all regions belonging to a country."""
    return COUNTRY_REGIONS.get(country_name, [])


def get_available_countries(available_regions):
    """Return sorted list of countries that have at least one region in the data."""
    countries = []
    for country, regions in COUNTRY_REGIONS.items():
        if any(r in available_regions for r in regions):
            countries.append(country)
    return sorted(countries)


def get_regions_for_countries(selected_countries, available_regions):
    """Return sorted list of regions for selected countries (or all if none selected).

    Args:
        selected_countries: List of selected country names (empty = show all)
        available_regions: Set of region names that exist in the data

    Returns:
        Sorted list of region names
    """
    if not selected_countries:
        return sorted(available_regions)

    regions = []
    for country in selected_countries:
        for r in COUNTRY_REGIONS.get(country, []):
            if r in available_regions:
                regions.append(r)
    return sorted(regions)


def resolve_country_region_selections(selected_countries, selected_regions, available_regions):
    """Resolve country + region selections into a final list of region names.

    If countries are selected but no regions, include all regions for those countries.
    If both are selected, use the explicit region selections.

    Args:
        selected_countries: List of selected country names
        selected_regions: List of selected region names
        available_regions: Set of all available region names in the data

    Returns:
        List of region names to filter by
    """
    if selected_regions:
        return list(selected_regions)
    if selected_countries:
        regions = []
        for country in selected_countries:
            for r in COUNTRY_REGIONS.get(country, []):
                if r in available_regions:
                    regions.append(r)
        return regions
    return []
