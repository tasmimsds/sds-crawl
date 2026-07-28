# Export duplicate-row diagnosis & fix

Dedup identity (matches the spec): a **TRUE duplicate** = same `(url, category, evidence quote)`.
Same URL with a *different* fact rule or *different* evidence = a distinct finding (kept). Same URL
across different category files = expected (kept).

## STEP 1 — Root cause (case 1: stored data, not a JOIN fan-out)

Not a JOIN issue: the export queries join `urls`/`products` 1:1 — no fan-out. Not locale variants
being mistaken for dups (`/us/x` vs `/uk/x` are different URLs, correctly separate).

The duplicates were **stored twice in `issues`**, all as one finding recorded under two rows:

- **AI-screen title carried the LLM's guessed `expected`.** The title was
  `llm:{cat}:{expected[:32]}`, and the table's `UNIQUE(url_id, category, title, related_url_id)`
  includes `title`. So when the AI flagged the *same quote* on the *same page* with two different
  `expected` guesses (e.g. `29Languages supported` → expected `32` and expected `36`), it produced
  **two rows with identical (url, category, evidence)** that the UNIQUE constraint let through.
- A handful more came from **rule renames** (old `region_count:inconsistent` fixed-row lingering
  next to the current `sdsmanager_countries:inconsistent` open-row on the same evidence) and
  analyzer evolution (`lang_ctx` vs `lang_unclear` on the same quote).

Example (au/about-us, language_count, evidence "29Languages supported"):
`llm:language_count:36` (expected 36) **and** `llm:language_count:32 (for SDS Man…)` — same quote,
two rows.

## STEP 2 — Fix at the source

1. **Writer fix (prevents recurrence):** the AI-screen title is now keyed on the *quote*
   (`llm:{cat}:{sha256(quote)[:12]}`), not the volatile `expected`. Re-runs now **upsert** the same
   quote instead of inserting a new row.
2. **Stored cleanup (idempotent, in `db.connect()` via `dedup_issues`):** collapses every
   `(url_id, category, evidence)` group to one row — keeps the **earliest-detected** row, makes it
   **open if any copy is open** (else unclear if any is), and carries the **latest last_checked_at**.
   Runs on startup; a no-op once clean. Rows removed are logged.
3. One identity — `(url, category, evidence)` — now governs the stored data, so the UI, counts, and
   every export (CSV + Excel) agree automatically (they all read the deduped `issues` table).

## STEP 3 — Presentation

- **Default (unchanged):** one row per distinct finding. After dedup, no two rows share
  `(url, category, evidence)`, so no identical rows appear; a URL still shows on multiple rows only
  when the rule/evidence differs.
- **Optional "one row per URL"** export (`?view=by_url`, CSV + Excel): aggregates a page to a single
  row with `issue_count`, `categories` (joined), top `severity`, and `evidences` (joined) — a
  page-level fix list. It **aggregates, never discards** a finding.

## STEP 4 — Verification

Rows removed: **105** (issues 776 → 671). Open findings 511 → **478** (−33 open; the other 72 removed
were non-open old-rule/fixed dup rows). **0 true-duplicate groups remain.** Zero distinct findings
lost (positioning 113 and free_claim 108 unchanged — they had no true dups; au/about-us kept both of
its *different-evidence* region_count findings).

| category | open before | open after | note |
|---|--:|--:|---|
| positioning | 113 | 113 | no true dups |
| free_claim | 108 | 108 | no true dups |
| database_size | 33 | 33 | dup rows were fixed-status |
| language_count | 169 | 148 | −21 AI double-flags |
| region_count | 65 | 53 | −12 AI/old-rule dups |

Per-category **DB open == CSV rows == XLSX rows** confirmed for all categories; the `by_url`
aggregate (233 URLs) and `full.xlsx` (Summary + per-category tabs) verified; all download links
resolve; UTF-8-BOM/hyperlinks/real-dates intact.
