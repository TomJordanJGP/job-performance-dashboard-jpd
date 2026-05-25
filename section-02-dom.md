# Client Report — Section 02 DOM container map

Captured from the live `streamlit run app.py` DOM via Chrome dev tools while
viewing a generated Client Report.

The example client used at capture time was Southwark Council; sizes (pixels)
will scale with viewport width but the hierarchy and `data-testid` chain
shown here is constant.

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ SECTION 02 CARD                    data-testid="stVerticalBlock"   1249 × 779  x=380 │
│ Styled by .has(.client-eyebrow) CSS rule — 1 px border, 8 px radius, 32/36 padding.  │
└─┬────────────────────────────────────────────────────────────────────────────────────┘
  │
  ├── HEADER (markdown blob)              stElementContainer       1175 × 130  x=417
  │   └── stMarkdown
  │       └── div .st-emotion-cache-6c7yup            ← Streamlit emotion wrapper
  │           └── stMarkdownContainer .st-emotion-cache-gc7n1q
  │               ├── .client-eyebrow                 "02 — Benchmarking"
  │               ├── .client-h2                      "Per-vacancy benchmarking"
  │               ├── .client-section-intro           (generic explainer)
  │               └── hr.client-section-divider       (2 px purple rule)
  │
  └── COLUMNS ROW                          stLayoutWrapper          1175 × 567  x=417
      └── stHorizontalBlock
          │
          ├── LEFT COLUMN (chart, 3fr)     stColumn                  697 × 567  x=417
          │   └── stVerticalBlock          (per-column inner block)
          │       │
          │       ├── CHART                stElementContainer        697 × 500
          │       │   └── stFullScreenFrame
          │       │       └── stPlotlyChart           ← the scatter
          │       │
          │       └── CAPTION              stElementContainer        697 ×  51
          │           └── stMarkdown
          │               └── stCaptionContainer
          │                   └── <p>                  "Each marker is one vacancy…"
          │
          └── RIGHT COLUMN (stats+notes, 2fr)  stColumn              462 × 567  x=1130
              └── stVerticalBlock          (per-column inner block)
                  │
                  ├── STATUS GRID          stElementContainer        462 × 274
                  │   └── stMarkdown
                  │       └── stMarkdownContainer
                  │           └── .status-grid (2 × 2)
                  │               ├── Benchmarkable
                  │               ├── Low traffic
                  │               ├── Possible redirect
                  │               └── No benchmark
                  │
                  └── COMMENTARY           stElementContainer        462 × 196
                      └── stMarkdown
                          └── stMarkdownContainer
                              └── .commentary-panel    (beige callout)
```

## Things to remember when touching this section

1. **The card itself is a `stVerticalBlock`**, not the old `stVerticalBlockBorderWrapper`.
   That test ID does not appear in this Streamlit version. The CSS rule that styles
   the card is:

   ```css
   [data-testid="stVerticalBlock"]:has(> [data-testid="stElementContainer"]:first-child .client-eyebrow) { … }
   ```

   "A `stVerticalBlock` whose first child is a markdown element-container that
   contains a `.client-eyebrow`" — uniquely matches sections 02-09 and excludes the
   page-level block, nested column blocks, and the settings card.

2. **The 37 px inset** between the card edge (x = 380) and the eyebrow text
   (x = 417) decomposes as `1 px border + 36 px padding`.

3. **Each Streamlit column wraps its own content in a nested `stVerticalBlock`**.
   Those nested blocks do NOT match the card CSS rule (their first child isn't a
   markdown with `.client-eyebrow`), so they remain unstyled.

4. **Two emotion-cache classes appear on every markdown block**:
   - `.st-emotion-cache-6c7yup` — outer markdown wrapper
   - `.st-emotion-cache-gc7n1q` — inner markdown container

   These rotate between Streamlit versions, so we don't target them directly.
   `.client-h2` / `.client-section-divider` / `.client-eyebrow` use `!important`
   on margin and padding to beat any default styling those emotion classes inject.

5. **`section_anchor('scatter')` was removed** from section 02 — the in-page TOC
   was dropped earlier so the invisible anchor `<span>` served no purpose. Other
   sections (01, 03-09) still carry their anchor calls; remove them if/when the
   user asks.
