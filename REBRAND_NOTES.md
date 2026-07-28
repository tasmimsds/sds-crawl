# Rebrand — "ExactFact Checker" by SDS Manager

Display/branding only — no internal identifiers, module names, DB, or package name changed.

## Logo assets (`static/img/`)
Copied from the uploads (not referenced from Downloads):
- `SDS_Manager_icon.png` (300×300 diamond-check mark) — favicon source, collapsed sidebar, app icon.
- `logo-full-yellow-light.avif` (500×167 mark + wordmark) — expanded sidebar, report headers.

Generated fallbacks (AVIF isn't universally supported):
- `logo-full.png` — PNG of the full logo (via `sips`), used in `<picture>` fallback + reports.
- `favicon.ico` — multi-size 16/32/48 (via Pillow) · `favicon-32.png` · `apple-touch-icon.png` (180) ·
  `icon-192.png` / `icon-512.png` (manifest).
- `static/site.webmanifest` — `name` "ExactFact Checker", `short_name` "ExactFact", themed `#626DF9`.

All static routes verified 200.

## Name applied
- `base.html`: default `<title>` → **ExactFact Checker**; every page keeps "{Page} — ExactFact Checker".
  Favicon / apple-touch-icon / manifest / `apple-mobile-web-app-title` / `theme-color` added to `<head>`.
- Sidebar brand: **icon (collapsed) / full logo (expanded)** with a "ExactFact Checker" product name in
  logo-blue over the SDS Manager logo (AVIF `<source>` + PNG `<img>` fallback).
- Empty state → "Add your first website to start fact-checking with ExactFact Checker".
- Footer on every page → "ExactFact Checker **by SDS Manager**".
- All per-page titles across `templates/` (Facts Library, Dashboard, Settings, Fact Check, Reports,
  Results, run detail, matrix, onboarding) → "… — ExactFact Checker".
- **HTML report** (`report.html` + `html_report.py`): title "ExactFact Checker — Fact Check Report",
  header with the full logo (embedded base64 data URI so it's self-contained) + "ExactFact Checker
  by SDS Manager" + generated date.
- **Excel Full report** (`xlsx_export.py`): Summary sheet A1 "ExactFact Checker", A2 "by SDS Manager ·
  Full fact-check report", plus the floating full logo (PNG).
- `README.md` title + description.

`home.html` still holds the old string but is **dead** (no route renders it — only `dashboard.html`
and `fact-check.html` are served), so it is not user-facing.

## Palette (minimal accent alignment)
`app.css :root`: `--primary` → **#626DF9** (the checkmark blue), `--primary-d` #4b55d9, `--accent`
#626DF9. Added `--gold` **#FFC93C** (the diamonds) with `--gold-soft` and an accessible `--gold-d`
(#8a6a00) for text/borders on white. Existing green pass/positive semantics kept for accessibility;
gold used as a secondary accent (e.g. the footer attribution). Static cache bumped to `?v=19`.

## Verified
- Tab title reads "ExactFact Checker"; favicon = diamond-check mark (link tags + .ico serve 200).
- Sidebar: full logo + "ExactFact Checker" expanded; icon-only when collapsed (<820px) — screenshot-confirmed.
- Generated HTML report shows the logo + "ExactFact Checker by SDS Manager" — screenshot-confirmed.
- Excel Full report Summary: A1/A2 branded, floating logo present.
- No leftover old brand string ("SDS Fact Check", "Crawling & Analyzing System", "Internal Audit
  Report") in any served template/JS/CSS.
- PNG fallback: full logo wrapped in `<picture>` (AVIF source → PNG img); reports use the PNG directly.
