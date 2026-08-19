# UI/UX systemic audit & fix

Audited every page with **real data** (crawled sdsmanager.com, 527 open findings, 15 backlinks,
8 general facts, long URLs/quotes) at **1440 / 1024 / 375 px**, using an automated overflow probe
(flag any element whose right edge exceeds the viewport and is *not* inside an intentional
`overflow-x:auto` scroller) plus visual review.

## STEP 0 — Root cause (what was actually wrong)

The base layout is **structurally sound** and was NOT the cause:
- One shared base template (`templates/base.html`): fixed-width sidebar + scrollable main, extended
  by every page. Not re-implemented per page.
- Sidebar/content use flexbox, not fixed-position: `.layout{display:flex}` ·
  `.sidebar{width:250px;flex-shrink:0}` · `.content{flex:1;min-width:0;overflow-x:hidden;width:100%}`
  — so content can shrink and never slides under the sidebar. Sidebar collapses to icons ≤820px.
- Z-index scale exists (`--z-dropdown/-sidebar/-modal/-toast`).
- Dashboard live sections **swap, not append**: the pollers (`applyPipeline`/`applyExternalPipeline`)
  set `textContent`/`className` on existing `[data-k]`/`[data-stage]` nodes — no `appendChild`/
  `innerHTML +=`, so 10+ poll cycles can't stack or duplicate.

**The real, demonstrable defects — two structural anti-patterns, not per-page bugs:**

1. **Action controls laid out as a wide data-table** (Reports page). "Findings by category" was a
   5-column `<table>` of CSV/Excel buttons. Tables can't reflow, so at ≤600px the button columns
   were **clipped at the card edge** (the "buttons overlap / collide, header doesn't line up"
   report). Worst offender, exactly as flagged.
2. **Toolbar rows that don't wrap.** `.import-form` (Facts Library) used `margin-left:auto` with no
   `flex-wrap`, so the file input + Import button **ran off the right edge** at 375px.

Everything else already reflowed (tables scroll inside their `overflow-x:auto` cards; toolbars
wrap; pipelines stack vertically ≤640px).

## STEP 1 — Foundation reinforced (shared, used app-wide)

Added to `static/app.css` (one place; no page styles its own layout):
- **Aligned page header** `.head-row` → `.titles` (left) + `.head-actions` (right), one baseline,
  wraps on mobile. Replaces ad-hoc inline `display:flex` headers.
- **Export-card system** `.export-section` / `.export-grid` (auto-fill `minmax(280px,1fr)` → single
  column ≤640px) / `.export-card` (name + meta + format buttons, always vertically aligned).
- **Button consistency**: `.btn{min-height:38px}`, `.btn-sm{min-height:32px}`, `.btn-group` (wrapping
  inline-flex) — no more ragged action rows.
- Confirmed z-index scale; sidebar/modal/toast all use the named scale.

## STEP 2 — Reports page (the worst) rebuilt

Wide button-table → **responsive card grid**, grouped with section headings:
- Header aligned: title/subtitle left, **Full report** primary action right (stacks under on mobile).
- **By category** — one card per category (name + "N open · N all" + CSV/Excel), reflows 3-col →
  1-col.
- **Whole project** — Full report (featured), All open, All matches, One row per URL.
- **External** — External fact mismatches + General Facts (new).
- **HTML report** — generate + file list.
- Result: at 375px every download item is a full-width card with tidy, equal-height CSV/Excel
  buttons. No clipping, no overlap, no horizontal page scroll.

## STEP 3–4 — Other pages & UX

`.import-form` now wraps and goes full-width ≤640px. All other pages already compose the base
layout + shared card/table/toolbar/badge/button and needed no structural change. Toasts
(`.toast`, `alertBanner`) are used for save/success; General Facts actions use them (no `alert()`
for success — `confirm()` is retained only as an intentional guard on destructive/irreversible
actions). Empty states present (Reports, General Facts, findings tables). Async sections update in
place.

## STEP 5 — Acceptance (automated overflow probe + visual, at 1440 / 1024 / 375)

| Page | 1440 | 1024 | 375 |
|---|---|---|---|
| Dashboard (Internal + External flows) | ✅ | ✅ | ✅ (flows stack) |
| Reports & Exports | ✅ | ✅ | ✅ (card grid → 1-col) |
| Results & Issues | ✅ | ✅ | ✅ (table scrolls in card) |
| General Facts (+ Backlinks tab) | ✅ | ✅ | ✅ |
| Fact Check | ✅ | ✅ | ✅ |
| Facts Library | ✅ | ✅ | ✅ (import-form fixed) |
| Settings | ✅ | ✅ | ✅ |
| Add Website | ✅ | ✅ | ✅ |

Pass = **no horizontal page scroll, zero elements overflowing outside an intentional scroller,
header aligned with content, buttons never overlap.** Probe result on every page at 375:
`pageScroll:false, genuineOffenders:0`.

**Not freshly re-verified (environmental):** a *live* 10-cycle Dashboard poll — starting an external
run needs the LLM, and the OpenRouter key is at its weekly spend limit (402). The swap-not-append
behavior is guaranteed at the code level (see STEP 0); no live stacking is possible by construction.

Files changed: `static/app.css` (foundation components + import-form fix), `templates/reports.html`
(rebuilt), `src/app.py` (reports route adds General Facts count), `templates/base.html` (cache
`v=24`).
