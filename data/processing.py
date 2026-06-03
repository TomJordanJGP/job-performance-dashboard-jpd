"""Data processing and enrichment functions."""

import re
import numpy as np
import pandas as pd


def apply_importer_mapping(df, mapping=None):
    """Ensure importer_name has no nulls. SQL is the primary source for importer names;
    this just fills any remaining gaps with 'Unknown'."""
    df = df.copy()
    if 'importer_name' not in df.columns:
        df['importer_name'] = 'Unknown'
    else:
        df['importer_name'] = df['importer_name'].fillna('Unknown')
    if 'importer_ID' in df.columns:
        df['importer_id_str'] = df['importer_ID'].astype(str).str.strip()
    return df


def parse_upgrades(df):
    """Parse upgrades column and create individual upgrade columns."""
    if 'upgrades' not in df.columns:
        return df

    df = df.copy()

    all_upgrades = set()
    for upgrades_str in df['upgrades'].dropna():
        if pd.notna(upgrades_str) and upgrades_str.strip():
            upgrades_list = [u.strip() for u in str(upgrades_str).split('|')]
            all_upgrades.update(upgrades_list)

    df['upgrades_list'] = df['upgrades'].apply(lambda x:
        [u.strip() for u in str(x).split('|')] if pd.notna(x) and str(x).strip() else []
    )

    return df


def categorise_media_source(source, medium, campaign):
    """Categorise a GA4 source/medium/campaign combination into a channel category.

    Rules are applied in priority order. Returns one of 20 categories.
    """
    s = str(source).strip().lower() if pd.notna(source) else ''
    m = str(medium).strip().lower() if pd.notna(medium) else ''
    c = str(campaign).strip().lower() if pd.notna(campaign) else ''

    # 1. Google Jobs
    if 'google_jobs_apply' in s or 'google_jobs_apply' in c:
        return 'Google Jobs'

    # 2. Client Career Page (ATS)
    if ('ats-' in s and '.jgp.co.uk' in s) or \
       'applyforthis.com' in s or \
       ('bsipjobs' in s and '.jgp.co.uk' in s):
        return 'Client Career Page'

    # 3. AI Chatbot
    ai_sources = [
        'chatgpt.com', 'copilot.com', 'copilot.microsoft.com',
        'copilot.cloud.microsoft', 'perplexity', 'perplexity.ai',
        'claude.ai', 'gemini.google.com', 'grok.com', 'manus.im',
    ]
    if s in ai_sources:
        return 'AI Chatbot'

    # 4. Direct
    if s == '(direct)' and m == '(none)':
        return 'Direct'

    # 5. Email / Job Alerts
    if m == 'email' or s == 'job_alert' or s == 'sendgrid.com' or \
       'email_campaign' in c:
        return 'Email / Job Alerts'

    # 6. Paid Search (PPC)
    if m in ('cpc', 'ppc') and s in ('google', 'adwords', 'bing', 'microsoft', 'jooble'):
        return 'Paid Search'
    # Handle malformed ppc entries like "ppc,ppc" or source with appended UTM params
    if 'ppc' in m and any(x in s for x in ('google', 'adwords', 'bing', 'microsoft')):
        return 'Paid Search'
    if 'cpc' in m and any(x in s for x in ('google', 'adwords', 'bing', 'microsoft')):
        return 'Paid Search'
    # Missing source but clearly PPC from medium + campaign
    if m in ('cpc', 'ppc') and (not s or s == 'nan') and \
       any(x in c for x in ('jgp-google', 'microsoft-ads')):
        return 'Paid Search'
    # Malformed source with UTM params appended (e.g. "google&utm_medium=ppc...")
    if s.startswith('google&utm_medium=ppc'):
        return 'Paid Search'

    # 7. Audio / Streaming
    if m == 'audio':
        return 'Audio / Streaming'

    # 8. LinkedIn Job Slots
    if m in ('job-slot', 'job-board'):
        return 'LinkedIn Job Slots'

    # 9. Social Media (Paid) - requires a campaign name to distinguish from organic
    if m in ('social', 'paid_social', 'paid') and c and c not in ('(not set)', ''):
        return 'Social Media (Paid)'
    # Social medium without campaign = organic social post
    if m in ('social', 'paid_social', 'paid') and (not c or c in ('(not set)', '')):
        return 'Social Media (Organic)'

    # 10. Job Aggregator
    aggregator_sources = [
        'talent', 'uk.talent.com', 'jobrapido', 'click.appcast.io',
        'idibu.com', 'jobisjob.co.uk',
    ]
    if m == 'aggregator' or m == 'search' or s in aggregator_sources:
        return 'Job Aggregator'

    # 11. Indeed
    if s == 'indeed' or c == 'indeed' or 'indeed.com' in s:
        return 'Indeed'

    # 12. JGP Partner Site
    partner_sources = [
        'goodwork.london', 'netzerocareers', 'netzerocareers.co.uk',
        'jobs-redefined.co', 'ucgjobs.com', 'seftoncounciljobs.co.uk',
        'liverpoolcityregionjobs.co.uk', 'seftonatwork.aptem.co.uk',
        'workhounslow.co.uk', 'innorthsomerset.co.uk',
        'tunbridgewells.works', 'timeforworthing.uk', 'ustsc.org.uk',
    ]
    if s in partner_sources:
        return 'JGP Partner Site'

    # 13. Other Job Board
    job_board_sources = [
        'totaljobs', 'totaljobs.com', 'adzuna.co.uk', 'ziprecruiter',
        'lgjobs.com', 'careerjet.co.uk', 'boltjobs.com', 'findjobuk.com',
        'myjobhelper.co.uk', 'remoteworker.co.uk', 'hellohiring.co.uk',
        'uk.jooble.org', 'rkycareers.com', 'gb.bebee.com',
        'eu.experteer.com', 'm.experteer.co.uk', 'us.experteer.com',
        'experteer.co.uk', 'metajob.de', 'generalist.world',
        'troopr.co.uk', 'jobflexlanka.com', 'destinydot.com',
    ]
    if s in job_board_sources:
        return 'Other Job Board'

    # 14. Niche / Sector Job Board
    niche_sources = [
        'artsjobs.org.uk', 'healthcareers.nhs.uk', 'cybersecurityjobsite.com',
        'energyjobsearch.com', 'oilandgasjobsearch.com', 'publiclawjobs.co.uk',
        'propertyweek4jobs.com', 'ehn-jobs.com', 'isepjobs.org',
        'conservation-careers.com', 'aghires.com', 'supplychainonline.co.uk',
        'newscientist.com', 'chambersstudent.co.uk', 'jobs.accaglobal.com',
        'jobs.aerosociety.com', 'jobs.cih.org', 'jobs.imarest.org',
        'cips.org', 'bps.org.uk', 'bacp.co.uk', 'w4mp.org',
        'clearing-house.org.uk', 'service-design-network.org',
        'nhsprocurement.org.uk', 'ipsgrow.org.uk', 'datatoinsight.org',
        'laria.org.uk', 'muslimsinpp.org',
    ]
    if s in niche_sources:
        return 'Niche / Sector Job Board'

    # 15. Social Media (Organic) - includes link shorteners mapped to parent platform
    social_sources = [
        'facebook.com', 'l.facebook.com', 'm.facebook.com', 'lm.facebook.com',
        'instagram.com', 'l.instagram.com', 'linkedin.com', 'lnkd.in',
        'l.threads.com', 'go.bsky.app', 'reddit.com', 'snapchat.com',
        'youtube.com', 'mumsnet.com', 'thestudentroom.co.uk',
        'link.zhihu.com', 'naver.com', 'm.blog.naver.com', 'blog.naver.com',
        't.co', 'hootsuite.com',
    ]
    if s in social_sources:
        return 'Social Media (Organic)'

    # 16. School Website
    school_sources = [
        'elmhurstprimary.co.uk', 'crowthornecofe.co.uk', 'aecps.org',
        'malbank.com', 'whybridge.co.uk', 'haveringadultcollege.co.uk',
        'eastcroftpark.co.uk',
    ]
    if s.endswith('.sch.uk') or s in school_sources:
        return 'School Website'

    # 17. University / Careers Service
    uni_extra = [
        'careerpilot.org.uk', 'prospects.ac.uk', 'icould.com',
        'nationalcareers.service.gov.uk', 'webchat.nationalcareers.service.gov.uk',
        'careerswales.gov.wales', 'mychoice16.co.uk', 'sheffieldprogress.co.uk',
        'cxk.org', 'careeredge.careercentre.me', 'mmu.careercentre.me',
        'et.careerhub.co.uk', 'intoo4you.your-latitude.com',
    ]
    if s.endswith('.ac.uk') or 'joinhandshake.co.uk' in s or \
       'targetconnect.net' in s or s in uni_extra:
        return 'University / Careers Service'

    # 18. Government / Council
    gov_extra = [
        'adph.org.uk', 'jobs.gov.fk', 'liia.london',
        'parksforlondon.org.uk', 'brighterfuturesforchildren.org',
        'joinus.birminghamchildrenstrust.co.uk',
    ]
    if s.endswith('.gov.uk') or s in gov_extra:
        return 'Government / Council'

    # 19. Organic Search
    search_engines = [
        'google', 'bing', 'yahoo', 'duckduckgo', 'ecosia.org', 'qwant.com',
        'yandex', 'yandex.ru', 'ya.ru', 'aol', 'startpage.com', 'avg',
    ]
    if m == 'organic' and s in search_engines:
        return 'Organic Search'
    # Search engines appearing as referral traffic
    search_referral_exact = [
        'startpage.com', 'ya.ru', 'yandex.ru', 'search.google.com',
    ]
    if s in search_referral_exact:
        return 'Organic Search'
    # Referral from search engine domains (pattern match)
    search_domain_patterns = [
        'search.yahoo.com', 'search.aol.', 'search.brave.com',
        'search.avastbrowser.com', 'search.avgbrowser.com',
        'syndicatedsearch.goog', 'search.offidocs.com',
        'search.becovi.net', 'search.seekters.com',
        'search.voicecommandsearcher.com', 'search.fanrealmadrid.com',
        'search.travelingleisure.com', 'search-dre.dt.dbankcloud.com',
        'search.riskscreen.com', 'find-searcher.com',
        'yellow-search.org', 'seek.ageful.com', 'karmasearch.org',
    ]
    if any(p in s for p in search_domain_patterns):
        return 'Organic Search'

    # 20. Referral (Other)
    return 'Referral (Other)'


def apply_media_categories(df):
    """Apply source_category to a media DataFrame with source/medium/campaign columns."""
    if not all(col in df.columns for col in ['source', 'medium', 'campaign']):
        return df
    df = df.copy()
    df['source_category'] = df.apply(
        lambda row: categorise_media_source(row['source'], row['medium'], row['campaign']),
        axis=1
    )
    return df


def prepare_enriched_data(df):
    """Prepare vacancy summary data by renaming columns for dashboard compatibility."""
    df = df.copy()
    column_mapping = {
        'entity_id_str': 'entity_id',
    }
    existing_renames = {k: v for k, v in column_mapping.items() if k in df.columns}
    df = df.rename(columns=existing_renames)
    return df


def add_occupation_column(df):
    """Extract occupation field from occupational_fields column."""
    if 'occupational_fields' in df.columns:
        df['occupation'] = df['occupational_fields'].apply(lambda x:
            str(x).split('|')[0].strip().title() if pd.notna(x) and str(x).strip() else 'Unknown'
        )
    else:
        df['occupation'] = 'Unknown'
    return df


def parse_dates_in_jobiqo(df):
    """Parse date columns from vacancy summary data."""
    if 'first_event_date' in df.columns:
        df['first_event_date'] = pd.to_datetime(df['first_event_date'], errors='coerce', utc=True).dt.tz_localize(None)
    if 'last_event_date' in df.columns:
        df['last_event_date'] = pd.to_datetime(df['last_event_date'], errors='coerce', utc=True).dt.tz_localize(None)
    if 'start_date' in df.columns:
        df['start_date'] = pd.to_datetime(df['start_date'], errors='coerce', utc=True).dt.tz_localize(None)
    if 'end_date' in df.columns:
        df['end_date'] = pd.to_datetime(df['end_date'], errors='coerce', utc=True).dt.tz_localize(None)
    return df


# ============================================================================
# SALARY PROCESSING FUNCTIONS
# ============================================================================

# Regex for currency symbols
_CURRENCY_RE = re.compile(r'[£$€]')
# Regex for a single monetary value: optional currency, digits with optional commas, optional k/K suffix
_VALUE_RE = r'[£$€]?\s*(\d[\d,]*\.?\d*)\s*([kK])?'
# Detect hourly/daily/weekly/monthly unit from text
_UNIT_PATTERNS = [
    (re.compile(r'per\s*hour|/\s*hr|/\s*hour|p\.?h\.?(?:\b|$)', re.I), 'hour'),
    (re.compile(r'per\s*day|/\s*day|p\.?d\.?(?:\b|$)', re.I), 'day'),
    (re.compile(r'per\s*week|/\s*week|p\.?w\.?(?:\b|$)', re.I), 'week'),
    (re.compile(r'per\s*month|/\s*month|p\.?m\.?(?:\b|$)|pcm', re.I), 'month'),
    (re.compile(r'per\s*annum|p\.?a\.?(?:\b|$)|annual', re.I), 'year'),
]


def _parse_value(match_str, k_suffix):
    """Convert a regex-captured value string to float."""
    val = float(match_str.replace(',', ''))
    if k_suffix and k_suffix.lower() == 'k':
        val *= 1000
    return val


def parse_salary_free_text(text):
    """Parse a human-readable salary string into structured components.

    Returns dict with keys: min_salary, max_salary, currency, unit.
    Any field may be None if not parseable.
    """
    if not text or not isinstance(text, str):
        return {'min_salary': None, 'max_salary': None, 'currency': None, 'unit': None}

    text = text.strip()
    if not text or text.lower() in ('competitive', 'negotiable', 'doe', 'see description',
                                     'not specified', 'n/a', 'tbc', 'tba', 'unpaid',
                                     'voluntary', 'volunteer'):
        return {'min_salary': None, 'max_salary': None, 'currency': None, 'unit': None}

    # Detect currency
    currency = None
    if '£' in text:
        currency = 'GBP'
    elif '$' in text:
        currency = 'USD'
    elif '€' in text or 'eur' in text.lower():
        currency = 'EUR'

    # Detect unit from text
    unit = None
    for pattern, unit_name in _UNIT_PATTERNS:
        if pattern.search(text):
            unit = unit_name
            break

    min_sal = None
    max_sal = None

    # Try range patterns: "£30k - £40k", "£30,000 to £40,000", "30000-40000"
    range_re = re.compile(
        _VALUE_RE + r'\s*(?:-|–|to)\s*' + _VALUE_RE, re.I
    )
    m = range_re.search(text)
    if m:
        min_sal = _parse_value(m.group(1), m.group(2))
        max_sal = _parse_value(m.group(3), m.group(4))
    else:
        # Try "up to £X" / "to £X"
        up_to_re = re.compile(r'(?:up\s+to|upto|max(?:imum)?)\s*' + _VALUE_RE, re.I)
        m = up_to_re.search(text)
        if m:
            max_sal = _parse_value(m.group(1), m.group(2))
        else:
            # Try "from £X" / "minimum £X"
            from_re = re.compile(r'(?:from|min(?:imum)?|starting)\s*' + _VALUE_RE, re.I)
            m = from_re.search(text)
            if m:
                min_sal = _parse_value(m.group(1), m.group(2))
            else:
                # Try single value
                single_re = re.compile(_VALUE_RE)
                m = single_re.search(text)
                if m:
                    val = _parse_value(m.group(1), m.group(2))
                    min_sal = val
                    max_sal = val

    # Infer unit from magnitude if not explicitly stated
    if unit is None and (min_sal or max_sal):
        ref = min_sal or max_sal
        if ref < 25:
            unit = 'hour'
        elif ref < 500:
            unit = 'day'
        else:
            unit = 'year'

    # Default currency to GBP (UK job board)
    if currency is None and (min_sal or max_sal):
        currency = 'GBP'

    return {'min_salary': min_sal, 'max_salary': max_sal, 'currency': currency, 'unit': unit}


def normalize_salary_to_annual(value, unit):
    """Convert a salary value to annual equivalent.

    Assumptions: 37.5 hrs/week, 52 weeks/year, 260 working days/year.
    """
    if value is None or np.isnan(value):
        return np.nan
    unit = str(unit).lower().strip() if unit else 'year'
    multipliers = {
        'hour': 1950,      # 37.5 * 52
        'hourly': 1950,
        'day': 260,
        'daily': 260,
        'week': 52,
        'weekly': 52,
        'month': 12,
        'monthly': 12,
        'year': 1,
        'annual': 1,
        'annum': 1,
        'annually': 1,
    }
    return value * multipliers.get(unit, 1)


def process_salary_columns(df):
    """Process salary columns: fill from free text, normalise to annual.

    Creates columns: annual_min_salary, annual_max_salary, annual_mid_salary,
    has_salary_data, salary_source.
    """
    df = df.copy()

    # Ensure salary columns exist (graceful if BigQuery table not yet updated)
    for col in ['min_salary', 'max_salary', 'salary_exact', 'salary_free_text',
                'salary_unit', 'currency_code']:
        if col not in df.columns:
            df[col] = np.nan if col in ('min_salary', 'max_salary', 'salary_exact') else ''

    # Track source of salary data
    df['salary_source'] = 'none'
    has_numeric = df['min_salary'].notna() | df['max_salary'].notna()
    df.loc[has_numeric, 'salary_source'] = 'numeric'

    # Fill from salary_exact where min/max are both missing
    exact_mask = df['min_salary'].isna() & df['max_salary'].isna() & df['salary_exact'].notna()
    df.loc[exact_mask, 'min_salary'] = df.loc[exact_mask, 'salary_exact']
    df.loc[exact_mask, 'max_salary'] = df.loc[exact_mask, 'salary_exact']
    df.loc[exact_mask, 'salary_source'] = 'numeric'

    # Fill from free text where still missing
    free_text_mask = df['min_salary'].isna() & df['max_salary'].isna() & df['salary_free_text'].notna()
    if free_text_mask.any():
        parsed = df.loc[free_text_mask, 'salary_free_text'].apply(parse_salary_free_text)
        parsed_df = pd.DataFrame(parsed.tolist(), index=parsed.index)

        # Fill min/max from parsed values
        fill_mask = parsed_df['min_salary'].notna() | parsed_df['max_salary'].notna()
        rows_to_fill = free_text_mask & fill_mask.reindex(df.index, fill_value=False)

        df.loc[rows_to_fill, 'min_salary'] = parsed_df.loc[rows_to_fill.loc[rows_to_fill].index, 'min_salary']
        df.loc[rows_to_fill, 'max_salary'] = parsed_df.loc[rows_to_fill.loc[rows_to_fill].index, 'max_salary']
        df.loc[rows_to_fill, 'salary_source'] = 'free_text'

        # Fill currency and unit from parsed if missing
        for idx in rows_to_fill[rows_to_fill].index:
            if pd.isna(df.at[idx, 'currency_code']) or str(df.at[idx, 'currency_code']).strip() == '':
                parsed_curr = parsed_df.at[idx, 'currency']
                if parsed_curr:
                    df.at[idx, 'currency_code'] = parsed_curr
            if pd.isna(df.at[idx, 'salary_unit']) or str(df.at[idx, 'salary_unit']).strip() == '':
                parsed_unit = parsed_df.at[idx, 'unit']
                if parsed_unit:
                    df.at[idx, 'salary_unit'] = parsed_unit

    # Determine effective unit per row
    def _effective_unit(row):
        unit = row.get('salary_unit')
        if pd.notna(unit) and str(unit).strip():
            return str(unit).strip().lower()
        # Infer from magnitude
        ref = row.get('min_salary') if pd.notna(row.get('min_salary')) else row.get('max_salary')
        if pd.notna(ref):
            if ref < 25:
                return 'hour'
            elif ref < 500:
                return 'day'
        return 'year'

    has_salary = df['min_salary'].notna() | df['max_salary'].notna()
    effective_units = df[has_salary].apply(_effective_unit, axis=1)

    # Normalise to annual
    df['annual_min_salary'] = np.nan
    df['annual_max_salary'] = np.nan

    for idx in effective_units.index:
        unit = effective_units[idx]
        if pd.notna(df.at[idx, 'min_salary']):
            df.at[idx, 'annual_min_salary'] = normalize_salary_to_annual(df.at[idx, 'min_salary'], unit)
        if pd.notna(df.at[idx, 'max_salary']):
            df.at[idx, 'annual_max_salary'] = normalize_salary_to_annual(df.at[idx, 'max_salary'], unit)

    # Filter unreasonable annual values
    for col in ['annual_min_salary', 'annual_max_salary']:
        unreasonable = (df[col] < 5000) | (df[col] > 500000)
        df.loc[unreasonable, col] = np.nan

    # Mid-point salary. NOTE: .mean(axis=1) skips NaN, so a one-sided range
    # collapses to its single known bound — "from £25k" (no max) becomes mid=£25k,
    # "up to £40k" (no min) becomes mid=£40k. That's a deliberate imputation (the
    # one advertised figure), but it does mix true midpoints with single bounds in
    # the benchmark distribution. If that bias matters, restrict the mid to rows
    # with BOTH bounds — a methodology change that would shift client-facing numbers.
    df['annual_mid_salary'] = df[['annual_min_salary', 'annual_max_salary']].mean(axis=1)

    # Boolean flag
    df['has_salary_data'] = df['annual_min_salary'].notna() | df['annual_max_salary'].notna()

    # Where all annual values got filtered out, reset source
    df.loc[~df['has_salary_data'], 'salary_source'] = 'none'

    return df


def calculate_salary_statistics(series):
    """Calculate salary summary statistics from a numeric Series (NaNs excluded)."""
    clean = series.dropna()
    if len(clean) == 0:
        return {k: 0 for k in ['count', 'mean', 'median', 'mode', 'p25', 'p75', 'min', 'max']}

    mode_val = clean.mode()
    return {
        'count': len(clean),
        'mean': clean.mean(),
        'median': clean.median(),
        'mode': mode_val.iloc[0] if len(mode_val) > 0 else clean.median(),
        'p25': clean.quantile(0.25),
        'p75': clean.quantile(0.75),
        'min': clean.min(),
        'max': clean.max(),
    }


def calculate_percentile_rank(value, series):
    """Calculate what percentile a value falls at within a Series."""
    clean = series.dropna()
    if len(clean) == 0:
        return 50.0
    return float((clean < value).mean() * 100)
