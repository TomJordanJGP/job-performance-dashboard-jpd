"""JGP brand color constants and Plotly chart template.

Tokens mirror brand-kit/Brand/tokens/jobs-go-public.tokens.json (v2026-05-01).
Every brand decision in the dashboard flows from this module — CSS, components,
Plotly templates and chart layouts all consume JGP_COLORS / JGP_LOGOS rather
than hardcoding hex literals.
"""

# Brand palette + semantic tokens (1:1 with tokens.json)
JGP_COLORS = {
    # Core brand palette
    'primary':      '#643791',  # Brand Purple — primary brand colour
    'accent':       '#e5ff6e',  # Bold Green — sparingly, CTAs / focus
    'supporting':   '#9c67d3',  # Secondary purple
    'light_purple': '#e8e0f2',  # Soft background
    'deep_blue':    '#240f45',  # Best dark digital background / primary text
    'deep_green':   '#2e4500',  # Supporting dark green
    'beige':        '#f0f3e1',  # Warm supporting BG (chart use)
    'pink':         '#ffc4c4',  # Supporting (sparingly)
    'light_blue':   '#defae8',  # Supporting (sparingly)
    'light_green':  '#d6dbb2',  # Muted green (chart use)
    'blue':         '#bad9e5',  # Supporting blue (used for NI in colorway)
    'white':        '#ffffff',
    'black':        '#000000',  # Avoid; prefer deep_blue for dark text

    # Semantic tokens (digital surfaces / interaction states)
    'text_primary':   '#240f45',
    'text_secondary': '#4f4360',
    'text_muted':     '#5b5067',
    'surface':        '#ffffff',
    'surface_warm':   '#f8f5fb',
    'border':         '#cbb9df',
    'focus_outer':    '#e5ff6e',
    'focus_inner':    '#240f45',

    # Functional / chart-state colours
    'positive': '#2e4500',  # Deep green — positive deltas
    'negative': '#c0392b',  # Red — negative deltas (only off-brand exception, kept
                            # for chart-state semantics; meaning is also conveyed by
                            # arrow + text label per WCAG 1.4.1).
    'neutral':  '#9c67d3',  # Supporting purple — neutral deltas
}

# Replaces previous off-brand button hover (#7b4aab). Brand supporting purple
# is the closest in-palette tone for a "primary slightly lighter" hover.
HOVER_PRIMARY = JGP_COLORS['supporting']

# Local asset paths (relative to repo root)
JGP_LOGOS = {
    'full_colour': 'assets/brand/jgp-logo-full-colour.svg',
    'white':       'assets/brand/jgp-logo-white.svg',
    'favicon':     'assets/brand/favicon.ico',
}

# Plotly chart template with JGP branding
JGP_PLOTLY_TEMPLATE = dict(
    layout=dict(
        font=dict(family="DM Sans, sans-serif", color=JGP_COLORS['deep_blue'], size=13),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        # Strict-brand colorway. Sequence chosen for max separation across
        # the first four series (the most common chart depth).
        colorway=[
            JGP_COLORS['primary'],       # Brand Purple
            JGP_COLORS['deep_green'],    # Deep Green
            JGP_COLORS['supporting'],    # Supporting Purple
            JGP_COLORS['blue'],          # Brand Blue (replaces off-brand amber)
            JGP_COLORS['light_purple'],  # Light Purple
            JGP_COLORS['deep_blue'],     # Deep Blue
        ],
        hoverlabel=dict(
            bgcolor=JGP_COLORS['deep_blue'],
            font_color=JGP_COLORS['white'],
            font_family="DM Sans, sans-serif",
        ),
        xaxis=dict(gridcolor=JGP_COLORS['light_purple'], gridwidth=1),
        yaxis=dict(gridcolor=JGP_COLORS['light_purple'], gridwidth=1),
        legend=dict(
            bgcolor="rgba(255,255,255,0.8)",
            bordercolor=JGP_COLORS['border'],
            borderwidth=1,
            font=dict(size=12),
        ),
        margin=dict(t=40, b=40, l=40, r=20),
    )
)

# Heatmap color scale (light to deep purple; runs through the brand)
JGP_HEATMAP_COLORSCALE = [
    [0.0,  JGP_COLORS['beige']],
    [0.25, JGP_COLORS['light_purple']],
    [0.5,  JGP_COLORS['supporting']],
    [0.75, JGP_COLORS['primary']],
    [1.0,  JGP_COLORS['deep_blue']],
]
