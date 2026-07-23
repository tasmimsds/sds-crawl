# Finding-count reconciliation & coverage audit

## Ground truth (direct DB queries, source_id=1 = sdsmanager.com)

### Crawl coverage
| metric | count |
|---|---|
| URLs in project (`in_source=1`) | 4,206 |
| URLs with a successful (2xx) latest crawl | 4,023 |
| URLs with extracted `body_text` (read) | 4,020 |
| Not fetched | 183 |

**The 183 "not fetched" are NOT a gap in the primary site:** they are `sdsmanager.no` (96)
and `sdsmanager.es` (87) — separate country **domains** recorded as hreflang alternates, plus 3
`/faq/documents/*` example files (non-HTML). The crawler only fetches the primary registrable
domain (`sdsmanager.com`), so `.no`/`.es` are correctly recorded-but-not-crawled. **The primary
site is fully crawled (4,023 / 4,023 sdsmanager.com pages) and current.** If `.no`/`.es` need
fact-checking they must be added as their own projects.

### fact_matches (verdict store)
positive 235 · issue 256 · unclear 203. Per-rule issue counts sum to 256 (free_policy 87,
positioning 65, sds_languages 37, countries 29, db_size 25, exactsds_languages 12, regulations 1).

## Problem A — 288 (CSV) vs 264 (UI): different definitions

- **CSV export** (`report/csv_export.py`) had **no status and no `deleted_at` filter** →
  `issues_language_count.csv` = **287** rows = every language_count issue of *any* status
  (open + fixed + false_positive + deleted). 287 ≈ the "288" seen.
- **UI** ("Fact Check" and "Results & Issues") both call `_count(FACT_CATS)` /
  `_issue_rows` = `status='open' AND deleted_at IS NULL` = **264** total open across all
  categories. The two UI surfaces share one query, so they always agree (264 == 264,
  intentional).
- So 288-vs-264 compared *one category, all statuses* (287) against *all categories, open*
  (264) — apples to oranges. Root cause: **CSV counted all statuses incl. deleted.**

**Fix:** `export_csv(scope='open')` (default) now applies `status='open' AND deleted_at IS NULL`
and writes `issues_<cat>_open.csv`; a separate `scope='all'` writes `issues_<cat>_all.csv`.
Verified: `issues_language_count_open.csv` = **59** == UI open language_count; per-category open
CSVs sum to the UI open total.

## Problem B — "too low" (264) → real cause was auto-fixed AI findings

Coverage was **not** under-crawled (primary site fully crawled). Deterministic detection matches
raw text (free-plan raw 93 vs 86 deterministic issues; positioning raw 96 vs 64; db-size consistent).

The real under-count: the `issues` table had **488 `fixed`** rows. Broken down by origin:
- **250 `llm:` (AI screening pass) findings** — the deepest fact check (paraphrase/nuance).
- 414 old/renamed-rule issues (`languages_supported`, `region_count`, `fact:`, `query:` …) —
  genuinely superseded when rules were renamed in the product-scoping refactor.

**Root cause:** when the app was simplified to "fact-check only", the sync's fact-match stage
**stopped running the AI screening pass** (`fact_check_llm`). But `reconcile_fixed` auto-marks any
open issue in a category *not re-detected this run* as `fixed` — so every subsequent sync marked
the 250 real AI findings "fixed", even though the pages still contain them. Spot-verify: **12/12**
sampled fixed-`llm:` findings still have their exact evidence in the current `body_text`
(e.g. "40 languages", "17 million+ SDS in 29 languages", "free version of SDS Manager").
Diagnosis: **fix detection/reconcile, no re-crawl needed** (site already complete & current).

**Fixes applied:**
1. `reconcile_fixed(..., methods=[...])` is now **detector-aware**: it only fixes issues whose
   `detection_method` actually ran this run. It never fixes AI-screen findings unless the screening
   pass completed.
2. The AI screening pass uses a distinct `detection_method='ai_screen'` so it can't be conflated
   with the deterministic/query detectors.
3. `fact_check_llm` was **re-added to the sync fact-match stage** (it *is* fact checking); it
   returns a `completed` flag and only counts toward reconciliation when it finished without
   pausing on the spend/key cap.
4. Restored the **247** still-valid AI findings to `open` (3 whose evidence is gone stayed fixed).

## Reconciled result

| surface | before | after |
|---|---|---|
| Open findings (Fact Check == Results) | 264 | **511** |
| `issues_language_count.csv` | 287 (all-status) | 59 open (`_open.csv`) / 287 (`_all.csv`) |
| Sum of per-category open CSVs | — | **511 == UI open** |

Open by category (after): language_count 169 · positioning 113 · free_claim 108 · region_count 65 ·
database_size 33 · other_mismatch 22 · regulation_count 1 = **511**.

**Spot-check (free-plan):** system open = **108**, naive phrase-grep = 93. System is *higher*
because the restored AI screening catches paraphrased free-plan claims a literal grep misses
(deterministic-only would be 86). So detection is now *broader* than raw text search, not narrower.

## Note / remaining lever
A **fresh** AI screening run (to catch changes since the last LLM pass) is currently blocked by the
OpenRouter **key weekly spend limit** (403). The historical still-valid AI findings are restored, and
the pass is wired back into the sync + cost-tracked; raise the key limit to regenerate on demand.
`.no`/`.es` are separate domains — add them as projects to fact-check them.
