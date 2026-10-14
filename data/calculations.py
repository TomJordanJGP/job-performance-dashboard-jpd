"""Metric calculation functions."""

import numpy as np


# Tooltip copy for the KPI tiles shared by the Dashboard and Performance
# tabs. Colocated with the calculations so the explanation and the formula
# never drift apart. The Low / Mid / Top 25% bands are always defined by
# clicks across every tile (see calculate_quartile_metrics) — the tooltips
# call that out explicitly so a reader doesn't assume each tile re-banks by
# its own metric.
KPI_TOOLTIPS = {
    'Total Vacancies': (
        "Count of unique vacancies with at least one event in the period. "
        "The Low / Mid / Top bands rank vacancies by clicks and count how "
        "many fall in each band."
    ),
    'Total Clicks': (
        "Sum of detail-page clicks across all vacancies. "
        "Bands rank vacancies by clicks and sum each band's clicks."
    ),
    'Total Applies': (
        "Sum of apply-button clicks. Bands rank vacancies by clicks "
        "(not applies) and sum each band's applies — so 'Top 25%' is the "
        "quarter with the most clicks, useful for asking whether high-"
        "traffic vacancies actually convert."
    ),
    'Apply/Click Rate': (
        "Applies ÷ Clicks × 100 — visitor-to-applicant conversion. "
        "Bands rank vacancies by clicks and compute each band's rate."
    ),
    'Avg Clicks / Vacancy': (
        "Total clicks ÷ total vacancies. Bands rank vacancies by clicks "
        "and show each band's average. A wide Top vs Low gap means traffic "
        "is concentrated on a small slice of postings."
    ),
    'Avg Applies / Vacancy': (
        "Total applies ÷ total vacancies. Bands rank vacancies by clicks "
        "(not applies) and show each band's per-vacancy apply count."
    ),
    'Total Impressions': (
        "Times the vacancies were shown: listed in Jobs Go Public / LG Jobs "
        "search results (on-site) plus their job page shown in Google search. "
        "Google impressions of the homepage, listing pages and blog aren't "
        "counted — they name no vacancy. Bands rank vacancies by clicks and sum "
        "each band's impressions."
    ),
    'Click-Through Rate': (
        "Clicks ÷ Impressions × 100. Clicks count every visit to the job page, "
        "including routes that aren't impressions (direct links, job boards, "
        "email alerts), so treat this as visits per showing. Bands rank "
        "vacancies by clicks and compute each band's rate."
    ),
    'Avg Impressions / Vacancy': (
        "Total impressions ÷ total vacancies. Bands rank vacancies by clicks "
        "and show each band's per-vacancy impressions."
    ),
}


def remove_outliers_iqr(data):
    """Remove outliers using IQR (Interquartile Range) method."""
    if len(data) < 4:
        return data

    q1 = np.percentile(data, 25)
    q3 = np.percentile(data, 75)
    iqr = q3 - q1

    lower_bound = q1 - (1.5 * iqr)
    upper_bound = q3 + (1.5 * iqr)

    return [x for x in data if lower_bound <= x <= upper_bound]


def calculate_metrics(df):
    """Calculate key metrics from pre-aggregated vacancy summary data."""
    metrics = {}
    metrics['num_vacancies'] = len(df)

    # Both columns must be present — callers pass arbitrary subsets, so guarding
    # on 'clicks' alone risked a KeyError on 'applies'. .sum() already skips NaN.
    has_metrics = 'clicks' in df.columns and 'applies' in df.columns
    if has_metrics:
        metrics['total_clicks'] = int(df['clicks'].sum())
        metrics['total_applies'] = int(df['applies'].sum())
    else:
        metrics['total_clicks'] = 0
        metrics['total_applies'] = 0

    metrics['apply_click_ratio'] = (metrics['total_applies'] / metrics['total_clicks'] * 100) if metrics['total_clicks'] > 0 else 0

    if metrics['num_vacancies'] > 0 and has_metrics:
        metrics['mean_clicks_per_vacancy'] = metrics['total_clicks'] / metrics['num_vacancies']
        metrics['mean_applies_per_vacancy'] = metrics['total_applies'] / metrics['num_vacancies']
        # nanmedian, not median: a NULL click/apply from BigQuery would otherwise
        # turn the median into NaN.
        metrics['median_clicks_per_vacancy'] = float(np.nanmedian(df['clicks'].values))
        metrics['median_applies_per_vacancy'] = float(np.nanmedian(df['applies'].values))
        metrics['clicks_per_vacancy'] = metrics['mean_clicks_per_vacancy']
        metrics['applies_per_vacancy'] = metrics['mean_applies_per_vacancy']
    else:
        metrics['median_clicks_per_vacancy'] = 0
        metrics['median_applies_per_vacancy'] = 0
        metrics['mean_clicks_per_vacancy'] = 0
        metrics['mean_applies_per_vacancy'] = 0
        metrics['clicks_per_vacancy'] = 0
        metrics['applies_per_vacancy'] = 0

    # Impressions are a later column tier (see data/loader.py); absent until t06
    # carries them, so the views check `has_impressions` before showing tiles.
    metrics['has_impressions'] = 'impressions' in df.columns
    if metrics['has_impressions']:
        for col in ('impressions', 'onsite_impressions', 'google_impressions',
                    'impressions_jgp', 'impressions_lg'):
            metrics[f'total_{col}'] = int(df[col].sum()) if col in df.columns else 0
        n, impr = metrics['num_vacancies'], metrics['total_impressions']
        metrics['click_through_rate'] = metrics['total_clicks'] / impr * 100 if impr > 0 else 0
        metrics['mean_impressions_per_vacancy'] = impr / n if n > 0 else 0
        metrics['median_impressions_per_vacancy'] = (
            float(np.nanmedian(df['impressions'].values)) if n > 0 else 0)

    return metrics


def calculate_quartile_metrics(df):
    """Calculate metrics by performance quartiles (top 25%, middle 50%, bottom 25%)."""
    if 'clicks' not in df.columns:
        return None

    if len(df) < 4:
        return None

    vacancy_clicks = df['clicks']
    vacancy_applies = df['applies']
    vacancy_impressions = df['impressions'] if 'impressions' in df.columns else None

    q1_threshold = vacancy_clicks.quantile(0.25)
    q3_threshold = vacancy_clicks.quantile(0.75)

    if q1_threshold < q3_threshold:
        top_25_mask = vacancy_clicks >= q3_threshold
        middle_50_mask = (vacancy_clicks >= q1_threshold) & (vacancy_clicks < q3_threshold)
        bottom_25_mask = vacancy_clicks < q1_threshold
    else:
        # Degenerate spread: when most vacancies share a click count (e.g. lots of
        # zero-traffic postings) q1 == q3, and value thresholds would dump every
        # vacancy into "Top 25%". Rank by position instead so the bands stay
        # ~25/50/25 and the headline tile isn't misleading.
        pct = vacancy_clicks.rank(method='first', pct=True)
        bottom_25_mask = pct <= 0.25
        middle_50_mask = (pct > 0.25) & (pct <= 0.75)
        top_25_mask = pct > 0.75

    quartiles = {}

    for name, mask in [('top_25', top_25_mask), ('middle_50', middle_50_mask), ('bottom_25', bottom_25_mask)]:
        total_clicks = int(vacancy_clicks[mask].sum())
        total_applies = int(vacancy_applies[mask].sum())
        num_vacancies = int(mask.sum())

        quartiles[name] = {
            'num_vacancies': num_vacancies,
            'total_clicks': total_clicks,
            'total_applies': total_applies,
            'apply_click_ratio': (total_applies / total_clicks * 100) if total_clicks > 0 else 0,
            'clicks_per_vacancy': total_clicks / num_vacancies if num_vacancies > 0 else 0,
            'applies_per_vacancy': total_applies / num_vacancies if num_vacancies > 0 else 0
        }
        if vacancy_impressions is not None:
            total_impressions = int(vacancy_impressions[mask].sum())
            quartiles[name].update({
                'total_impressions': total_impressions,
                'click_through_rate': (total_clicks / total_impressions * 100) if total_impressions > 0 else 0,
                'impressions_per_vacancy': total_impressions / num_vacancies if num_vacancies > 0 else 0,
            })

    return quartiles
