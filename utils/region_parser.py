"""
UK Region Parser - Extract regions from addresses
"""

import re
import pandas as pd

# UK regions and their common identifiers
UK_REGIONS = {
    'Greater London': ['london', 'greater london', 'ec1', 'ec2', 'ec3', 'ec4', 'wc1', 'wc2', 'n1', 'nw1', 'se1', 'sw1', 'e1', 'w1'],
    'South East': ['surrey', 'kent', 'sussex', 'berkshire', 'hampshire', 'oxfordshire', 'buckinghamshire', 'brighton', 'reading', 'slough',
                   'farnham', 'bicester', 'bracknell', 'upminster', 'macclesfield', 'kings hill', 'warfield', 'camberley', 'cheshunt',
                   'guildford', 'crawley', 'basingstoke', 'eastbourne', 'hastings', 'canterbury', 'maidstone', 'ashford', 'tunbridge wells'],
    'South West': ['bristol', 'devon', 'cornwall', 'dorset', 'somerset', 'gloucestershire', 'wiltshire', 'bath', 'exeter', 'plymouth',
                   'swindon', 'bournemouth', 'poole', 'taunton', 'gloucester', 'cheltenham'],
    'East of England': ['essex', 'hertfordshire', 'bedfordshire', 'cambridgeshire', 'norfolk', 'suffolk', 'luton', 'norwich', 'cambridge',
                        'hertford', 'stevenage', 'watford', 'st albans', 'chelmsford', 'colchester', 'ipswich', 'peterborough'],
    'East Midlands': ['leicestershire', 'nottinghamshire', 'derbyshire', 'lincolnshire', 'northamptonshire', 'leicester', 'nottingham', 'derby',
                      'northampton', 'lincoln', 'mansfield'],
    'West Midlands': ['birmingham', 'coventry', 'wolverhampton', 'warwickshire', 'worcestershire', 'staffordshire', 'shropshire', 'herefordshire',
                      'dudley', 'walsall', 'solihull', 'west bromwich', 'stoke', 'telford', 'worcester', 'hereford', 'shrewsbury'],
    'Yorkshire and The Humber': ['yorkshire', 'leeds', 'sheffield', 'bradford', 'hull', 'york', 'doncaster', 'wakefield',
                                  'barnsley', 'rotherham', 'huddersfield', 'halifax', 'harrogate', 'scarborough'],
    'North West': ['manchester', 'liverpool', 'lancashire', 'cheshire', 'merseyside', 'cumbria', 'preston', 'bolton', 'blackpool',
                   'crewe', 'widnes', 'chester', 'birkenhead', 'knutsford', 'neston', 'warrington', 'stockport', 'oldham', 'rochdale',
                   'salford', 'wigan', 'blackburn', 'burnley', 'carlisle', 'lancaster'],
    'North East': ['newcastle', 'sunderland', 'durham', 'tyne and wear', 'northumberland', 'tees', 'middlesbrough', 'gateshead',
                   'darlington', 'hartlepool', 'stockton'],
    'Scotland': ['edinburgh', 'glasgow', 'aberdeen', 'dundee', 'inverness', 'scotland', 'scottish', 'stirling', 'perth', 'paisley'],
    'Wales': ['cardiff', 'swansea', 'newport', 'wales', 'welsh', 'cymru', 'wrexham', 'bangor', 'aberystwyth'],
    'Northern Ireland': ['belfast', 'northern ireland', 'derry', 'lisburn', 'newry', 'armagh'],
}

# UK postcode area to region mapping
POSTCODE_REGIONS = {
    # London
    'E': 'Greater London', 'EC': 'Greater London', 'N': 'Greater London', 'NW': 'Greater London',
    'SE': 'Greater London', 'SW': 'Greater London', 'W': 'Greater London', 'WC': 'Greater London',

    # South East
    'BR': 'South East', 'CR': 'South East', 'DA': 'South East', 'GU': 'South East',
    'KT': 'South East', 'ME': 'South East', 'RG': 'South East', 'RH': 'South East',
    'SL': 'South East', 'SM': 'South East', 'TN': 'South East', 'TW': 'South East',
    'OX': 'South East', 'HP': 'South East', 'MK': 'South East', 'BN': 'South East',
    'PO': 'South East', 'SO': 'South East', 'SP': 'South East',

    # South West
    'BA': 'South West', 'BS': 'South West', 'DT': 'South West', 'EX': 'South West',
    'GL': 'South West', 'PL': 'South West', 'TA': 'South West', 'TQ': 'South West',
    'TR': 'South West', 'SN': 'South West',

    # East of England
    'CB': 'East of England', 'CM': 'East of England', 'CO': 'East of England',
    'IP': 'East of England', 'LU': 'East of England', 'NR': 'East of England',
    'PE': 'East of England', 'SG': 'East of England', 'SS': 'East of England',

    # East Midlands
    'DE': 'East Midlands', 'DN': 'East Midlands', 'LE': 'East Midlands',
    'LN': 'East Midlands', 'NG': 'East Midlands', 'NN': 'East Midlands',

    # West Midlands
    'B': 'West Midlands', 'CV': 'West Midlands', 'DY': 'West Midlands',
    'HR': 'West Midlands', 'ST': 'West Midlands', 'SY': 'West Midlands',
    'TF': 'West Midlands', 'WR': 'West Midlands', 'WS': 'West Midlands',
    'WV': 'West Midlands',

    # Yorkshire
    'BD': 'Yorkshire and The Humber', 'DN': 'Yorkshire and The Humber',
    'HD': 'Yorkshire and The Humber', 'HG': 'Yorkshire and The Humber',
    'HU': 'Yorkshire and The Humber', 'HX': 'Yorkshire and The Humber',
    'LS': 'Yorkshire and The Humber', 'S': 'Yorkshire and The Humber',
    'WF': 'Yorkshire and The Humber', 'YO': 'Yorkshire and The Humber',

    # North West
    'BL': 'North West', 'CA': 'North West', 'CH': 'North West',
    'CW': 'North West', 'FY': 'North West', 'L': 'North West',
    'LA': 'North West', 'M': 'North West', 'OL': 'North West',
    'PR': 'North West', 'SK': 'North West', 'WA': 'North West',
    'WN': 'North West',

    # North East
    'DH': 'North East', 'DL': 'North East', 'NE': 'North East',
    'SR': 'North East', 'TS': 'North East',

    # Scotland
    'AB': 'Scotland', 'DD': 'Scotland', 'DG': 'Scotland', 'EH': 'Scotland',
    'FK': 'Scotland', 'G': 'Scotland', 'HS': 'Scotland', 'IV': 'Scotland',
    'KA': 'Scotland', 'KW': 'Scotland', 'KY': 'Scotland', 'ML': 'Scotland',
    'PA': 'Scotland', 'PH': 'Scotland', 'TD': 'Scotland', 'ZE': 'Scotland',

    # Wales
    'CF': 'Wales', 'LD': 'Wales', 'LL': 'Wales', 'NP': 'Wales',
    'SA': 'Wales', 'SY': 'Wales',

    # Northern Ireland
    'BT': 'Northern Ireland',
}


def extract_postcode_area(address):
    """Extract UK postcode area from address string."""
    if not address or pd.isna(address):
        return None

    # Look for UK postcode pattern (e.g., "SW1A 1AA", "M1 1AA")
    postcode_pattern = r'\b([A-Z]{1,2}\d{1,2}[A-Z]?)\s*\d[A-Z]{2}\b'
    match = re.search(postcode_pattern, str(address).upper())

    if match:
        postcode_area = match.group(1)
        # Remove any trailing letters for matching
        postcode_area = re.sub(r'[A-Z]$', '', postcode_area)
        return postcode_area

    return None


def extract_region_from_address(address):
    """
    Extract UK region from address string.
    Handles formats like "England, City, GB" or "State, City, Country"

    Args:
        address: String containing UK address

    Returns:
        UK region name or 'Unknown'
    """
    if not address or pd.isna(address):
        return 'Unknown'

    address_str = str(address).strip()
    address_lower = address_str.lower()

    # Handle "England, City, GB" format from Jobiqo export
    if ',' in address_str:
        parts = [p.strip() for p in address_str.split(',')]
        # Format is typically: State/Region, City, Country
        if len(parts) >= 2:
            city = parts[1].lower().strip()
            # Check if city is not empty
            if city:
                # Try to match city name to region
                for region, keywords in UK_REGIONS.items():
                    if city in keywords:
                        return region

    # First, try to extract and match postcode
    postcode_area = extract_postcode_area(address)
    if postcode_area and postcode_area in POSTCODE_REGIONS:
        return POSTCODE_REGIONS[postcode_area]

    # If no postcode match, try keyword matching on full address
    for region, keywords in UK_REGIONS.items():
        for keyword in keywords:
            if keyword in address_lower:
                return region

    return 'Unknown'


def add_region_column(df, address_column='regions'):
    """
    Add a UK region column to a dataframe based on address column.

    Args:
        df: Pandas dataframe
        address_column: Name of column containing addresses

    Returns:
        DataFrame with new 'uk_region' column
    """
    df = df.copy()
    df['uk_region'] = df[address_column].apply(extract_region_from_address)
    return df


def get_region_summary(df, address_column='regions'):
    """Get summary statistics of regions in the data."""
    df_with_region = add_region_column(df, address_column)
    summary = df_with_region['uk_region'].value_counts()
    return summary
