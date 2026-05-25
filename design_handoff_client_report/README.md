# Handoff: Client Advertising Report (web view)

## Overview
A continuous, scrollable web version of the Job Performance Dashboard "Client Report" tab. The current production page is a wall of text; this redesign organises the same data into nine numbered sections with a sticky in-page TOC, an editable settings panel at the top, and on-brand chart styling. Built against the **Jobs Go Public** brand kit (DM Sans, brand purple, deep blue, bold green, light purple).

## About the design files
The files in this bundle are **design references created in HTML** — a prototype showing intended look and behaviour, not production code to copy directly. The task is to **recreate this HTML design in the target codebase's existing environment** (React, Vue, etc.) using its established patterns, component library, and data layer. Wire the existing dashboard data to the section components rather than reusing the inline mock arrays.

## Fidelity
**High-fidelity.** Final colours, typography, spacing, layout and interactions. The developer should reproduce it pixel-fairly using the codebase's existing libraries and patterns. Any chart numbers and salary distributions in the HTML are placeholders transcribed from the original PowerPoint export — replace with live data.

## Screens / views
This is a single continuous page. Sections, in order:

1. **Sticky app bar** — full JGP logo (hosted PNG, 150px), divider, "Performance dashboard" label, top nav (Dashboard / Performance / Compare / Salary benchmarking / Client report — active), account-manager pill on the right.
2. **Collapsed left rail** (64 px, fixed) — represents the dashboard's primary sidebar auto-hidden on this tab. Five icon buttons + an expand chevron at the bottom. Hidden under 780 px.
3. **Report settings** — editable form: Client select, Report Period, Annual Spend, Rate Card Price, Include-self-in-benchmark toggle, then a Contact Details block (Account manager / Title / Email / Phone). Collapses on "Generate Report" or "Cancel" into a slim summary bar with an Edit button.
4. **Hero** — light-purple band: eyebrow + dot, h1 client name + period, lede paragraph, four-up meta row (Client / Reporting period / Vacancies in scope / Account manager).
5. **Two-column body**: sticky TOC (left) + report sections (right). Sections are:
   - 01 Headline numbers — 4 KPI cards (one filled deep blue)
   - 02 Per-vacancy benchmarking — scatter SVG + status grid + commentary
   - 03 Performance vs market — indexed bar chart + commentary
   - 04 Postings & apply volume — dual horizontal bars (posts vs applies)
   - 05 Advertising ROI — 4 KPIs + saving-bar visualisation
   - 06 Cost per apply — two horizontal-bar lists (cheapest / priciest) + collapsed full breakdown
   - 07 Salary benchmarks — 10 small-multiple histograms with three vertical reference lines
   - 08 Channel performance — table with mini bars and conversion column
   - 09 Export CTA — deep-blue panel, primary CTA + ghost button + white logo
6. **Tweaks panel** — bottom-right floating; toggles density (comfortable/compact), commentary visibility, accent colour. Optional in production.

## Interactions & behaviour
- **Settings collapse/expand**: clicking "Edit" on the collapsed bar (or the bar itself) reopens; "Cancel" or "Generate Report" collapses and scrolls to top.
- **TOC scrollspy**: IntersectionObserver with rootMargin `-40% 0px -55% 0px` highlights the section currently in view.
- **Cost-per-apply full breakdown**: native `<details>` disclosure.
- **Hover**: rows in the channel table tint to `--jgp-purple-soft`. Buttons/links use the brand focus ring (3 px deep-blue outline + 6 px green halo).
- **Responsive**: TOC + grid collapse to a single column under 980 px; left rail and 4-up KPI grids collapse under 780 px.
- **Tweaks protocol**: posts `__edit_mode_available` to parent; reacts to `__activate_edit_mode` / `__deactivate_edit_mode`; persists changes via `__edit_mode_set_keys`. Drop entirely if the parent app doesn't host this protocol.

## State management
For a real implementation, the following state is needed:
- `clientId`, `reportPeriod` (start/end), `annualSpendGBP`, `rateCardPerJobGBP`, `includeSelfInBenchmark`
- `accountManager.{name,title,email,phone}` (saved with the report)
- Derived: `costPerApply`, `costPerJob`, `costPerView`, `rateCardEquivalent`, `savingPct`
- Per-section data fetched by client + period: headline counts, indexed views/applies, per-vacancy benchmark deltas, postings × applies by job type, CPA by job type, salary distributions per occupation, channel rollup
- UI: `settingsCollapsed: bool`, `tocActiveId: string`

## Design tokens (from brand kit)

### Colours
| Token | Hex | Usage |
|---|---|---|
| `--jgp-purple` | `#643791` | Primary brand. Bars, accents, headings highlight |
| `--jgp-deep-blue` | `#240f45` | Primary text, dark panels, KPI dark card, export CTA |
| `--jgp-green` (bold green) | `#e5ff6e` | CTAs, accent line, focus halo |
| `--jgp-green-hover` | `#d6dbb2` | CTA hover |
| `--jgp-light-purple` | `#e8e0f2` | Hero bg, soft panels, light pills |
| `--jgp-purple-mid` | `#cbb9df` | Borders / rules |
| `--jgp-purple-soft` | `#f8f5fb` | Page bg, hover tint |
| `--jgp-beige` | `#f0f3e1` | Commentary panels |
| Pink | `#ffc4c4` | Salary "your mean" line |
| Deep green | `#2e4500` | Positive metrics text |
| Muted text | `#5b5067` | Secondary labels |

Salary chart reference lines: pink `#ffc4c4` (your), bold green `#e5ff6e` (national), deep blue `#240f45` (region) — solid 2 px.

### Typography
- **Family**: DM Sans (Google Fonts; weights 300/400/500/600/700)
- **Body**: 16 px / 1.55, letter-spacing 0
- **Helper**: 13 px
- **h1**: 40 px / 1.1, weight 700
- **h2**: 26 px / 1.2, weight 700
- **h3**: 18 px / 1.25, weight 700
- **h4 (eyebrow)**: 13 px, weight 600, brand-purple, **no uppercase**
- All sentence case. Never force uppercase. Never apply negative letter-spacing.

### Radii, spacing, elevation
- Card/panel/button radius: **8 px** (per brand)
- Pill radius: 999 px
- Page max-width: 1280 px; content padding 32 px
- Body padding-left: 64 px (left rail)
- Section gap: 64 px top margin per `<section class="block">`
- Button min-height: **44 px**
- `--shadow-sm`: `0 1px 2px rgba(36,15,69,.05)`
- `--shadow-md`: `0 8px 24px -16px rgba(36,15,69,.18), 0 2px 6px rgba(36,15,69,.04)`

### Focus state (brand spec)
```css
outline: 3px solid #240f45;
outline-offset: 3px;
box-shadow: 0 0 0 6px #e5ff6e;
```

## Assets
- **Logo (full colour)**: `https://media.jobsgopublic.com/wp-content/uploads/2025/10/JGP-Logo_RGB.png` (used in app bar)
- **Logo (white)**: `https://media.jobsgopublic.com/wp-content/uploads/2026/05/JGP-Logo_WHT.png` (export CTA footer)
- Local SVG copies are in `assets/` for self-hosted environments. Note: the supplied SVGs ship without inline fill rules — if hosting locally, either inline a `<style>` block setting `.cls-1{fill:#e5ff6e}` and `.cls-2{fill:#240f45}` or stick with the hosted PNGs.
- **Font**: DM Sans via Google Fonts `<link>` (or self-host the woff2 files listed in the brand kit's `fonts/dm-sans/`).
- **Font Awesome Pro** (per brand kit): kit ID `7a2f142c9b` — replace the inline SVG icons in the left rail with the equivalent FA icons if the kit is already loaded site-wide.

## Files in this bundle
- `Client Report.html` — full prototype, single-file (CSS + JS inline). Contains all section markup and the Tweaks panel.
- `tweaks-panel.jsx` — the Tweaks-panel React shell imported by the prototype. Optional — drop if the parent app doesn't expose the tweaks protocol.
- `assets/jgp-logo-colour.svg`, `assets/jgp-logo-white.svg` — local logo copies.

## Implementation notes
- Charts in the prototype are **hand-drawn SVG** (scatter, indexed bars, salary histograms, dual bars, ROI saving-bar). In a real app, use the existing chart library (Recharts / Highcharts / etc.) and apply the brand palette above. The histogram pattern (bars + 3 reference lines + axis labels) is the only non-trivial chart spec.
- The current production page is mentioned as the existing "Client Report" tab — this prototype is intended to replace its body content; the surrounding dashboard chrome (real top nav, real sidebar with auto-hide) lives in the parent app and should be reused, not reimplemented.
- All measurements, colours, typography and copy are in the prototype and the tables above. Inline copy can be lifted directly.
- WCAG 2.2 AA target. All text/background pairings used here are from the brand kit's verified accessible-pairings list; the brand-purple and deep-blue text both pass on white and light-purple backgrounds. Salary-chart line colours rely on position and shape, not colour alone — keep the legend visible.
