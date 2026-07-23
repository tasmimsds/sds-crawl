# Export audit — every downloadable file

Verified against the live DB (source_id=1, sdsmanager.com). Open = `status='open' AND deleted_at IS NULL`.

## Was `language_count` already fixed?

**Partly.** The *previous* reconciliation task fixed the **generator** (`export_csv` gained
`scope='open'/'all'`, so `issues_language_count_open.csv` = the UI open count). BUT two things
were still broken and are **fixed now**: (1) the old all-status file `issues_language_count.csv`
(288 rows) was **still on disk and still listed** by the Reports page (it globbed `*.csv`), so you
kept seeing 288; (2) downloads were served **static from disk** (could be stale). Both resolved:
orphan/stale files deleted, downloads regenerate **live** from the DB, UTF-8 BOM added.

## Inventory & status

| Export | Generator | Scope / query | Status |
|---|---|---|---|
| `issues_<cat>_open.csv` (per category) | `build_issue_csv` (unified) | open, not-deleted, per (url,rule) | 🔧 **Fixed now** — unified generator, live, UTF-8 BOM, scope in filename+header. lang_count generator was ✅ last task |
| `issues_<cat>_all.csv` | `build_issue_csv(scope='all')` | all non-deleted statuses, clearly named `_all` | 🔧 Fixed now |
| "Export all findings (open)" | `build_issue_csv(category=None)` | all categories, open | 🔧 New, unified |
| "Export all matches" | `build_issue_csv(scope='all')` | all categories, all statuses | 🔧 New, clearly named |
| `/results/export.csv` (filtered) | `build_rows_csv` | mirrors the Results advanced filter exactly | 🔧 Fixed now — BOM + shared columns |
| `external_findings_<scope>.csv` | `build_external_csv` | factcheck, scope-aware | 🔧 Fixed now — was all-status, no BOM; now scoped+BOM+live |
| `detected_claims.csv` (claim inventory) | `export_claims` (CLI) | value-frequency inventory (not a findings count) | ✅ Correct purpose; 🔧 added BOM |
| HTML report `report_source1_<date>.html` | `generate_html` | timestamped, regenerated each export | ✅ Already correct; clean of SEO |
| `facts_export.yaml` | `/facts/export` | fact rules dump (YAML) | ✅ Already correct |
| `issues_seo_technical.csv` | — | removed feature | 🔧 **Deleted** (orphan) |
| `issues_cannibalization.csv` | — | removed feature | 🔧 **Deleted** (orphan) |
| `issues_<cat>.csv` (no suffix ×9) | old `export_csv` | stale all-status disk files | 🔧 **Deleted** (superseded by `_open`/`_all`) |

## Error classes checked (STEP 2)

1. **Count/scope vs UI** — was the core bug: CSVs had no status/`deleted_at` filter. Now every
   export uses the same definition as the UI; scope is in the filename **and** a header comment row.
2. **Stale/regenerated** — was static disk files. Now all web CSVs are **generated live from the DB
   on each download** (`/reports/download.csv`, `/results/export.csv`); nothing served from disk.
3. **Row integrity** — consistent columns (url, locale, product, category, severity, status,
   fact_rule, evidence, expected, detection_method, detected_at). **0 open issues with empty
   evidence.** UTF-8 **BOM** so Excel renders de/hi/jp/el correctly; `csv` module handles
   quotes/commas/newlines in evidence.
4. **Removed features** — `seo_technical` / `cannibalization` CSVs deleted; no generator references them.
5. **Dead links** — Reports page no longer globs disk; it lists categories with **live** download
   links. All links resolve 200.

## STEP 5 verification (live)

| category | DB open | CSV rows | UI open |
|---|--:|--:|--:|
| language_count | 169 | 169 | 169 |
| free_claim | 108 | 108 | 108 |
| database_size | 33 | 33 | 33 |

- UTF-8 BOM present; file parses with `utf-8-sig`; columns intact; non-ASCII (de/hi) evidence readable.
- Links resolve: `/reports`, `/reports/download.csv?category=…&scope=open|all`, `?scope=open|all`,
  `/reports/download-external.csv`, `/results/export.csv` → all 200.

## Triplicate follow-up (positioning / region_count / regulation_count)

**Diagnosis.** The bare `issues_<cat>.csv` files were the *old* `export_csv` output (all statuses,
no scope filter, no BOM) — created before scope labels existed. When `_open`/`_all` were added, the
bare files were left behind, so a category briefly had three files. The bare file was effectively a
**duplicate of `_all`** (all-status), just without the scope label. `/output` is git-ignored, so
these were local disk artifacts only.

**Resolution.**
- ❌ **Removed as duplicate**: every bare `issues_<cat>.csv` (deleted; **no code path writes a bare
  name** — `csv_export.py` only emits `issues_{cat}_{scope}.csv`, so it can't recur).
- 🔧 Regenerated the disk set fresh from the current DB — now **exactly two files per category**
  (`_open`, `_all`); categories with 0 open issues (faq, feature_claim) have only `_all`.
- Web downloads regenerate **live** on click regardless of disk (Reports page never lists disk files).

**Standard (all categories):** `issues_<cat>_open.csv` (open, deduped per url+rule, == UI) and
`issues_<cat>_all.csv` (all non-deleted statuses, superset, has `status` column). No bare name.

**Product scoping (regulation_count):** verified correct — the analyzer expects **8** in ExactSDS
context and **49** for SDS Manager. The crawl has **0** ExactSDS-context regulation mentions, so the
single open issue is correctly `product=SDS Manager, expected=49`; an "8 regulations" claim on an
ExactSDS page would be treated as CORRECT (no issue), and as wrong for SDS Manager.

### Final reconciled table (live)

| category | DB open | `_open` rows | `_all` rows | UI count |
|---|--:|--:|--:|--:|
| positioning | 113 | 113 | 113 | 113 |
| region_count | 65 | 65 | 100 | 65 |
| regulation_count | 1 | 1 | 2 | 1 |

All BOM-verified (parse with `utf-8-sig`), 11 columns, 0 malformed rows, all download links 200.

## Remaining decisions for you (⚠)

- None blocking. Optional: the `_all` (all-statuses) exports include `fixed`/`false_positive` rows by
  design — kept as the explicit "all matches" export. Say if you'd prefer them removed entirely.
