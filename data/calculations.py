"""Metric calculation functions."""

import numpy as np


# Tooltip copy for the six KPI tiles shared by the Dashboard and Performance
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

    if 'clicks' in df.columns:
        metrics['total_clicks'] = int(df['clicks'].sum())
        metrics['total_applies'] = int(df['applies'].sum())
    else:
        metrics['total_clicks'] = 0
        metrics['total_applies'] = 0

    metrics['apply_click_ratio'] = (metrics['total_applies'] / metrics['total_clicks'] * 100) if metrics['total_clicks'] > 0 else 0

    if metrics['num_vacancies'] > 0 and 'clicks' in df.columns:
        metrics['mean_clicks_per_vacancy'] = metrics['total_clicks'] / metrics['num_vacancies']
        metrics['mean_applies_per_vacancy'] = metrics['total_applies'] / metrics['num_vacancies']
        metrics['median_clicks_per_vacancy'] = float(np.median(df['clicks'].values))
        metrics['median_applies_per_vacancy'] = float(np.median(df['applies'].values))
        metrics['clicks_per_vacancy'] = metrics['mean_clicks_per_vacancy']
        metrics['applies_per_vacancy'] = metrics['mean_applies_per_vacancy']
    else:
        metrics['median_clicks_per_vacancy'] = 0
        metrics['median_applies_per_vacancy'] = 0
        metrics['mean_clicks_per_vacancy'] = 0
        metrics['mean_applies_per_vacancy'] = 0
        metrics['clicks_per_vacancy'] = 0
        metrics['applies_per_vacancy'] = 0

    return metrics


def calculate_quartile_metrics(df):
    """Calculate metrics by performance quartiles (top 25%, middle 50%, bottom 25%)."""
    if 'clicks' not in df.columns:
        return None

    if len(df) < 4:
        return None

    vacancy_clicks = df['clicks']
    vacancy_applies = df['applies']

    q1_threshold = vacancy_clicks.quantile(0.25)
    q3_threshold = vacancy_clicks.quantile(0.75)

    top_25_mask = vacancy_clicks >= q3_threshold
    middle_50_mask = (vacancy_clicks >= q1_threshold) & (vacancy_clicks < q3_threshold)
    bottom_25_mask = vacancy_clicks < q1_threshold

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

    return quartiles
