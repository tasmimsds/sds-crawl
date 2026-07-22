# UI Audit & Fix Pass

Stack note: the app uses a single vendored stylesheet (`static/app.css`) + Jinja
templates (no Tailwind). Tailwind class names in the brief are applied as their
CSS equivalents. Backend logic / routes / data were NOT changed — only templates,
CSS, and presentation-only Jinja filters (`short_url`, `cat_label`, `pretty_title`,
`comma`).

Inspected at 1440 / 1024 / 375 px with real data (4,017 pages, 1,376 open issues,
long URLs, a 55-result fact). Status: ✅ fixed.

## Global (root causes — fixed once, apply everywhere)
- ✅ **Horizontal overflow on narrow screens** — `.cards-grid` used `minmax(340px,1fr)`, wider than the mobile content area → page scrolled / cards clipped under nothing. Fixed: `minmax(min(100%,300px),1fr)` + `.content{overflow-x:hidden}` + `min-width:0` on flex children.
- ✅ **Long URLs stretch layouts** — added `short_url` filter (host dropped, middle-truncated) + `.url` single-line ellipsis with full URL on hover; `break-word` fallback.
- ✅ **Raw internal labels shown** (`free_claim`, `free_policy:stale`, `fact:Free plan`) — added `cat_label` + `pretty_title` filters → human text everywhere.
- ✅ **Tables** — `table-layout:fixed`, per-column widths, cell truncation + tooltip, sticky header, zebra rows, `overflow-x:auto` container, empty-state row.
- ✅ **z-index scale** defined once (sidebar 30, dropdown 20, modal 50, toast 60).
- ✅ **Evidence quotes** — dedicated `.evidence` block (left border, muted bg, highlighted mark) instead of raw text in a cell.
- ✅ **Numbers** — `comma` filter (1,247); relative time already had absolute-on-hover.
- ✅ **line-clamp** utilities for titles/descriptions; badges unified (one scale).

## Dashboard
- ✅ site-card grid clipped at 375px → grid fix above.
- ✅ auto-sync inputs spilled on mobile → `.sched-form` wraps, inputs shrink.
- ✅ long sitemap location in card → clamped, `break-word`.
- ✅ history table wide → fixed layout + truncation + horizontal scroll inside card.
- ✅ live sync progress: polling swaps the same nodes (JS updates by id, no append) — no stacking.

## Fact Check + fact results
- ✅ term chips wrap (flex-wrap), never overflow the card.
- ✅ results list: capped display to 60 items with "showing N of M" note (was up to 300).
- ✅ match cards: url truncated, quote in `.evidence` block, expected + actions row.
- ✅ confirm card constrained (`max-width`), chips wrap.

## Results & Issues / Site Health
- ✅ URL column no longer wraps to 3 lines (truncate + tooltip).
- ✅ category + issue title humanized; severity badge unified.
- ✅ filter bar wraps on small widths.
- ✅ expanded detail uses evidence block; fixed table layout; sticky header; zebra.

## Facts Library
- ✅ rule/feature descriptions clamped to 2 lines; long term lists don't stretch cells.
- ✅ toolbar wraps; action buttons aligned.

## Reports / Settings / Add Website / Run detail
- ✅ Reports: file link lists wrap; export feedback inline.
- ✅ Settings: key/value tables aligned, help text styled.
- ✅ Add Website: card constrained, error message styled (not a browser alert).
- ✅ Run detail: error-page table URLs truncated + tooltip.

## Acceptance (1440/1024/375, real data)
- ✅ zero horizontal page scroll · zero overlap · nothing under the sidebar
- ✅ long content truncated with tooltip/expand
- ✅ consistent header/spacing/badges/buttons
- ✅ empty + loading states present
- ✅ sync progress stable across poll cycles
- ✅ no raw JSON / field names / unformatted numbers visible
