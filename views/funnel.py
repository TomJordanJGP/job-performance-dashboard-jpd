"""Application Funnel page — the first-party ATS recruitment journey + channel comparison.

Reads t06_summary_application_funnel (grain: vacancy x media_supplier_key) for the
ATS-tracked vacancies in the current filter selection. The ATS layer is kept SEPARATE
from the GA4 clicks/applies on the other tabs: the ATS already excludes Jobs Go Public
and LG Jobs traffic, so JGP appears here as one channel measured by GA4 (our
impressions -> clicks), next to the ATS channels (list views -> advert views).
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from data.filters import apply_filters_to_data
from theme.components import kpi_card, page_header, filter_tags, section_header, branded_divider
from theme.colors import JGP_COLORS, JGP_PLOTLY_TEMPLATE

_TOP_SUPPLIERS = 8          # entry channels shown individually in the Sankey (rest -> "Other")
_TABLE_MIN_APPS = 10        # min submitted applications for a channel to rank in the hires table
_COMPARE_ROWS = 15          # channels listed individually in the comparison (rest -> "Other")
_CTR_MIN_LIST_VIEWS = 1000  # min impressions / list views for a channel's CTR bar
_CTR_MIN_LIST_TABLE = 100   # below this, or with more views than list views, no CTR is shown:
                            # the channel's views mostly don't come from its listings
_JGP = "Jobs Go Public + LG Jobs (GA4)"


def _fmt(v):
    try:
        return f"{int(round(v)):,}"
    except (ValueError, TypeError):
        return "0"


def _rate(part, whole):
    """part ÷ whole as a percentage, or None when whole is 0 (shown as '—')."""
    return part / whole * 100 if whole else None


def _by_channel(fdf):
    """ATS metrics per channel. Grouped on media_supplier_key so case/punctuation
    variants of one supplier merge; labelled with its most common display name."""
    g = fdf.groupby('media_supplier_key', dropna=False)
    out = g.agg(
        list_views=('list_views', 'sum'),
        advert_views=('advert_views', 'sum'),
        apply_clicks=('apply_clicks', 'sum'),
        submitted=('n_submitted', 'sum'),
        hired=('n_hired', 'sum'),
        unsuccessful=('n_unsuccessful', 'sum'),
        withdrawn=('n_withdrawn', 'sum'),
        in_progress=('n_in_progress', 'sum'),
    )
    out['channel'] = g['media_supplier'].agg(
        lambda s: s.mode().iat[0] if s.notna().any() else '').replace('', '(unknown)')
    return out.reset_index(drop=True)


def _channel_comparison(fdf, ats_vac):
    """One row per channel — JGP from GA4 + our impressions, every other channel from
    the ATS — with like-for-like rates: views ÷ impressions (JGP) or ÷ list views."""
    chans = _by_channel(fdf).sort_values('advert_views', ascending=False)
    rows = [{
        'Channel': c.channel, 'Impressions / list views': c.list_views, 'Views': c.advert_views,
        'Apply clicks': c.apply_clicks, 'Applications': c.submitted, 'Hires': c.hired,
    } for c in chans.head(_COMPARE_ROWS).itertuples()]
    # JGP: the ATS excludes it, so applications/hires per JGP referral aren't known.
    jgp_impr = ats_vac['impressions'].sum() if 'impressions' in ats_vac.columns else None
    rows.append({
        'Channel': _JGP, 'Impressions / list views': jgp_impr, 'Views': ats_vac['clicks'].sum(),
        'Apply clicks': ats_vac['applies'].sum(), 'Applications': None, 'Hires': None,
    })
    table = pd.DataFrame(rows).sort_values('Views', ascending=False, kind='stable')
    rest = chans.iloc[_COMPARE_ROWS:]
    if len(rest):  # the long tail sits under the ranked channels, above the total
        table = pd.concat([table, pd.DataFrame([{
            'Channel': f"Other channels ({len(rest)})",
            'Impressions / list views': rest['list_views'].sum(), 'Views': rest['advert_views'].sum(),
            'Apply clicks': rest['apply_clicks'].sum(), 'Applications': rest['submitted'].sum(),
            'Hires': rest['hired'].sum(),
        }])], ignore_index=True)

    total = {
        'Channel': 'All channels',
        'Impressions / list views': table['Impressions / list views'].sum(min_count=1),
        'Views': table['Views'].sum(),
        'Apply clicks': table['Apply clicks'].sum(),
        'Applications': table['Applications'].sum(min_count=1),
        'Hires': table['Hires'].sum(min_count=1),
    }
    table = pd.concat([table, pd.DataFrame([total])], ignore_index=True)
    table['CTR %'] = [_rate(v, i) if pd.notna(i) and i >= _CTR_MIN_LIST_TABLE and v <= i else None
                      for v, i in zip(table['Views'], table['Impressions / list views'])]
    table['Apply rate %'] = [_rate(a, v) for a, v in zip(table['Apply clicks'], table['Views'])]
    table['App-to-hire %'] = [_rate(h, s) if pd.notna(s) else None
                              for h, s in zip(table['Hires'], table['Applications'])]
    return table[['Channel', 'Impressions / list views', 'Views', 'CTR %', 'Apply clicks',
                  'Apply rate %', 'Applications', 'Hires', 'App-to-hire %']]


def render_funnel(df, funnel_df=None):
    """Render the Application Funnel page (first-party ATS journey)."""
    if st.session_state.get('global_filters'):
        st.markdown(filter_tags(st.session_state.global_filters), unsafe_allow_html=True)
    st.markdown(page_header("Application Funnel"), unsafe_allow_html=True)
    st.caption(
        "First-party recruitment data from the ATS for ATS-tracked vacancies: every "
        "distribution channel's views, apply clicks, applications and outcomes. Kept separate "
        "from the Clicks / Applies on the other tabs, which are Jobs Go Public + LG Jobs only."
    )

    if funnel_df is None or len(funnel_df) == 0:
        st.info("ATS application-funnel data isn't available yet — it appears once the "
                "`t06_summary_application_funnel` table is built and loaded.")
        return

    # One vacancy set for both sides: the global filters run once over the vacancy table,
    # and the ATS rows and the JGP (GA4) row both cover exactly those ATS-tracked vacancies.
    filtered = apply_filters_to_data(df, st.session_state.get('global_filters'))
    fdf = funnel_df[funnel_df['entity_id_str'].isin(filtered['entity_id'])]
    ats_vac = filtered[filtered['entity_id'].isin(fdf['entity_id_str'])]
    if len(fdf) == 0:
        st.warning("No ATS-tracked vacancies match the current filters.")
        return

    # Aggregate the current selection
    agg = {c: int(fdf[c].sum()) for c in (
        'advert_views', 'apply_clicks', 'n_submitted', 'n_hired',
        'n_unsuccessful', 'n_withdrawn', 'n_in_progress')}
    adv, clk, app = agg['advert_views'], agg['apply_clicks'], agg['n_submitted']
    hired = agg['n_hired']
    tth_n = fdf['n_time_to_hire'].sum()
    avg_tth = (fdf['sum_time_to_hire_days'].sum() / tth_n) if tth_n else None

    def _pct(part, whole):
        return f"{(part / whole * 100):.1f}% of " if whole else "0.0% of "

    # KPI row (ATS channels) — consistent stage-conversion sub-lines so all boxes match height
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(kpi_card("ATS Advert Views", _fmt(adv),
                             helper=f"{ats_vac['entity_id'].nunique():,} ATS-tracked vacancies"),
                    unsafe_allow_html=True)
    with c2:
        st.markdown(kpi_card("ATS Apply Clicks", _fmt(clk),
                             helper=f"{_pct(clk, adv)}views"), unsafe_allow_html=True)
    with c3:
        st.markdown(kpi_card("Applications", _fmt(app),
                             helper=f"{_pct(app, clk)}apply clicks"), unsafe_allow_html=True)
    with c4:
        st.markdown(kpi_card("Hires", _fmt(hired),
                             helper=f"{_pct(hired, app)}applications"), unsafe_allow_html=True)

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # --- Channel comparison: JGP (GA4) next to the ATS channels ---
    st.markdown(section_header("Channel comparison", "bar-chart-line"), unsafe_allow_html=True)
    table = _channel_comparison(fdf, ats_vac)
    no_list = fdf.loc[fdf['list_views'] == 0, 'advert_views'].sum()
    st.caption(
        "Same vacancies on every row. Jobs Go Public + LG Jobs is measured by GA4: our "
        "impressions (on-site search results + Google) → clicks. Every other channel comes "
        "from the ATS: list views (shown in that channel's listings) → advert views. "
        "Applications and hires from JGP referrals aren't in the ATS, so show '—'. "
        f"{_rate(no_list, adv) or 0:.0f}% of ATS views come from channels with no list views "
        "(newsletters, social), so the All channels CTR reads slightly high. CTR is shown "
        f"where a channel has at least {_CTR_MIN_LIST_TABLE} list views and no more views "
        "than list views."
    )

    def _highlight(row):
        if row['Channel'] == _JGP:
            return ['background-color: rgba(100,55,145,0.14); font-weight: 600'] * len(row)
        if row['Channel'] == 'All channels':
            return ['font-weight: 600; border-top: 1px solid #999'] * len(row)
        return [''] * len(row)

    # Formatted here ('—' for not-applicable) because st.dataframe shows a Styler's
    # missing values as 'None' regardless of na_rep. Rows keep the order set above.
    counts = ['Impressions / list views', 'Views', 'Apply clicks', 'Applications', 'Hires']
    rates = ['CTR %', 'Apply rate %', 'App-to-hire %']
    shown = table.copy()
    for c in counts:
        shown[c] = [f"{v:,.0f}" if pd.notna(v) else '—' for v in shown[c]]
    for c in rates:
        shown[c] = [f"{v:.1f}%" if pd.notna(v) else '—' for v in shown[c]]
    st.dataframe(shown.style.apply(_highlight, axis=1), width='stretch', hide_index=True)

    bars = table[(table['Channel'] != 'All channels')
                 & ~table['Channel'].str.startswith('Other channels')
                 & (table['Impressions / list views'].fillna(0) >= _CTR_MIN_LIST_VIEWS)
                 ].dropna(subset=['CTR %'])
    if len(bars):
        bars = bars.sort_values('CTR %')
        fig = go.Figure(go.Bar(
            x=bars['CTR %'], y=bars['Channel'], orientation='h',
            marker_color=[JGP_COLORS['primary'] if c == _JGP else 'rgba(100,55,145,0.35)'
                          for c in bars['Channel']],
            text=[f"{v:.1f}%" for v in bars['CTR %']], textposition='outside',
        ))
        fig.update_layout(
            **JGP_PLOTLY_TEMPLATE['layout'],
            height=max(260, 34 * len(bars) + 80),
            xaxis_title='Click-through rate (views ÷ impressions or list views)',
            yaxis_title=None, showlegend=False,
        )
        st.plotly_chart(fig, width='stretch')
        st.caption(f"Channels with at least {_CTR_MIN_LIST_VIEWS:,} impressions / list views.")

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # --- Funnel Sankey: ATS channels -> views -> clicks -> applications -> outcomes ---
    st.markdown(section_header("The Funnel (ATS channels)", "funnel"), unsafe_allow_html=True)
    st.caption(
        "ATS media channels flow into advert views, then apply clicks, then applications, then "
        "the final outcome. Grey flows are the drop-off at each stage. "
        + (f"Average time to hire: {avg_tth:.0f} days. " if avg_tth else "")
        + "Note the top of the funnel is far larger than the recruitment stages, so later "
        "flows are thin."
    )

    sup = _by_channel(fdf)
    sup = sup[sup['advert_views'] > 0].sort_values('advert_views', ascending=False)
    top_sup = sup.head(_TOP_SUPPLIERS)
    other_views = int(sup['advert_views'].iloc[_TOP_SUPPLIERS:].sum()) if len(sup) > _TOP_SUPPLIERS else 0
    sup_labels = list(top_sup['channel'])
    sup_views = [int(v) for v in top_sup['advert_views']]
    if other_views > 0:
        sup_labels.append("Other channels")
        sup_views.append(other_views)

    S = len(sup_labels)
    V, C, A = S, S + 1, S + 2                 # Advert Views, Apply Clicks, Applications
    NC, NA = S + 3, S + 4                      # drop-off sinks
    HIRED, UNSUCC, WITHD, INPROG = S + 5, S + 6, S + 7, S + 8
    labels = sup_labels + ["Advert Views", "Apply Clicks", "Applications",
                           "No apply click", "Left before applying",
                           "Hired", "Unsuccessful", "Withdrawn", "In progress"]

    # Node positions: successful spine runs along the TOP (views -> clicks ->
    # applications -> hired); each drop-off peels to the BOTTOM and stops at its
    # stage's column. Order of the 9 fixed nodes: V, C, A, NC, NA, Hired, Unsucc,
    # Withdrawn, In-progress.
    node_x = [0.001] * S + [0.28, 0.52, 0.76, 0.52, 0.76, 0.999, 0.999, 0.999, 0.999]
    node_y = ([0.04 + 0.55 * (i / max(S - 1, 1)) for i in range(S)]
              + [0.14, 0.09, 0.05, 0.90, 0.90, 0.04, 0.80, 0.56, 0.30])

    primary = JGP_COLORS['primary']
    green = JGP_COLORS['deep_green']
    node_colors = ([primary] * S
                   + [primary, primary, primary, '#b6b6b6', '#b6b6b6',
                      green, '#c0392b', '#e67e22', '#8a6fb0'])
    STAGE = 'rgba(100,55,145,0.35)'
    DROP = 'rgba(150,150,150,0.28)'
    src, tgt, val, lcol = [], [], [], []

    for i, v in enumerate(sup_views):                      # channels -> Advert Views
        src.append(i); tgt.append(V); val.append(v); lcol.append('rgba(100,55,145,0.22)')
    src.append(V); tgt.append(C); val.append(clk); lcol.append(STAGE)              # views -> clicks (top)
    if adv - clk > 0:
        src.append(V); tgt.append(NC); val.append(adv - clk); lcol.append(DROP)    # views drop-off (bottom)
    src.append(C); tgt.append(A); val.append(app); lcol.append(STAGE)              # clicks -> apps (top)
    if clk - app > 0:
        src.append(C); tgt.append(NA); val.append(clk - app); lcol.append(DROP)    # clicks drop-off (bottom)
    for cnt, node, color in [                                                       # apps -> outcomes, best first
        (hired, HIRED, 'rgba(46,69,0,0.60)'),
        (agg['n_in_progress'], INPROG, 'rgba(138,111,176,0.50)'),
        (agg['n_withdrawn'], WITHD, 'rgba(230,126,34,0.45)'),
        (agg['n_unsuccessful'], UNSUCC, 'rgba(192,57,43,0.45)'),
    ]:
        if cnt > 0:
            src.append(A); tgt.append(node); val.append(cnt); lcol.append(color)

    sk = go.Figure(go.Sankey(
        arrangement="snap",
        # Crisp, dark, larger label font — the Plotly default (small + inherits the
        # brand font) renders muddy over the busy diagram.
        textfont=dict(color='#1a1a1a', size=14, family='Arial, Helvetica, sans-serif'),
        node=dict(label=labels, color=node_colors, x=node_x, y=node_y,
                  pad=22, thickness=18, line=dict(color='rgba(0,0,0,0.15)', width=0.5)),
        link=dict(source=src, target=tgt, value=val, color=lcol),
    ))
    sk.update_layout(**JGP_PLOTLY_TEMPLATE['layout'])
    # Right margin so the outcome labels (Hired/Unsuccessful/…) aren't clipped.
    sk.update_layout(height=580, margin=dict(l=10, r=120, t=10, b=10),
                     font=dict(family='Arial, Helvetica, sans-serif', size=14, color='#1a1a1a'))
    st.plotly_chart(sk, width='stretch')

    st.markdown(branded_divider(), unsafe_allow_html=True)

    # --- Channel table, ranked by application-to-hire rate ---
    st.markdown(section_header("Channels by application-to-hire rate", "table"), unsafe_allow_html=True)
    st.caption(f"ATS channels with at least {_TABLE_MIN_APPS} submitted applications, ranked by the "
               "share of applications that became a hire.")
    by_sup = _by_channel(fdf)
    by_sup = by_sup[by_sup['submitted'] >= _TABLE_MIN_APPS].copy()
    by_sup['app_to_hire'] = (by_sup['hired'] / by_sup['submitted'] * 100).round(1)
    by_sup = by_sup.sort_values('app_to_hire', ascending=False).head(20)
    tbl = by_sup.rename(columns={
        'channel': 'Channel', 'advert_views': 'Advert Views', 'apply_clicks': 'Apply Clicks',
        'submitted': 'Applications', 'hired': 'Hires', 'app_to_hire': 'App-to-Hire %',
        'unsuccessful': 'Unsuccessful', 'withdrawn': 'Withdrawn', 'in_progress': 'In Progress'})
    if len(tbl) == 0:
        st.info(f"No channel has {_TABLE_MIN_APPS}+ submitted applications in the current selection.")
    else:
        st.dataframe(
            tbl[['Channel', 'App-to-Hire %', 'Hires', 'Applications', 'Apply Clicks',
                 'Advert Views', 'Unsuccessful', 'Withdrawn', 'In Progress']],
            width='stretch', hide_index=True,
            column_config={
                'App-to-Hire %': st.column_config.NumberColumn(format="%.1f%%"),
                'Hires': st.column_config.NumberColumn(format="%,d"),
                'Applications': st.column_config.NumberColumn(format="%,d"),
                'Apply Clicks': st.column_config.NumberColumn(format="%,d"),
                'Advert Views': st.column_config.NumberColumn(format="%,d"),
                'Unsuccessful': st.column_config.NumberColumn(format="%,d"),
                'Withdrawn': st.column_config.NumberColumn(format="%,d"),
                'In Progress': st.column_config.NumberColumn(format="%,d"),
            },
        )
