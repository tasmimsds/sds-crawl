# Dedup verification — were multi-fact URLs wrongly reduced?

## Clear answer
- **Were distinct findings wrongly reduced?** **YES** — the first dedup pass used a too-coarse key
  and deleted ~72 distinct finding rows.
- **Is it now corrected?** **YES** — the findings were restored, the dedup key is fixed to the
  spec's `(url, fact_rule, evidence)`, 0 true duplicates remain, and 123 URLs correctly keep
  multiple distinct findings. Evidence below.

## STEP 1 — Key that was used
- **First (bad) dedup key:** `(url_id, category, evidence)` — **omitted `fact_rule`**. This is the
  dangerous case: when two *different* fact rules produced the *same* evidence quote in the same
  category (e.g. a deterministic rule and the AI screen on the identical sentence), it collapsed
  them into one, deleting a distinct finding.
- **Writer/UPSERT key:** `UNIQUE(url_id, category, title, related_url_id)` — fine (title encodes the
  rule); the AI-screen title is now `llm:<cat>:<sha(quote)>` so re-runs upsert instead of duplicating.

## STEP 2 — Loss detection
- Re-ran the deterministic engines (`analyze_facts_regex`, `consistency_check`,
  `analyze_product_claims`) — idempotent, content unchanged, so this reproduces the canonical
  finding set. It **regenerated 72 rows that were absent** (total 671 → 743; open 478 → 527).
  That absence is the proof the coarse key removed distinct findings.
- No clean pre-dedup DB backup existed (`data/crawl.db.bak` is from Jul 21, pre-dating this work),
  so restoration was done by regeneration, not rollback — valid because the source content is still
  crawled/stored and detection is deterministic.

## STEP 3 — Fix
1. **Restored** the deleted distinct findings by re-running deterministic fact matching (record_issue
   upserts recreated them). AI-screen findings (214) were untouched throughout.
2. **Corrected the dedup key** to `(url, fact_rule, evidence)` via `_fact_rule_of(title)`, which
   strips the volatile `expected`/hash so it collapses only *same-rule, same-quote* rows (the real
   AI double-flags) and **never** two different rules. Re-applied: **0 rows removed** (already clean),
   confirming nothing further needed collapsing and nothing distinct was at risk.
3. Writer + dedup + exports now share one identity, so re-runs can't collapse distinct findings again.

## STEP 4 — Evidence

| metric | after coarse dedup (bad) | after restore + correct dedup |
|---|--:|--:|
| total findings | 671 | **743** |
| open findings | 478 | **527** |
| true dups by (url, fact_rule, evidence) | (n/a) | **0** |
| URLs with >1 distinct open finding | 114 | **123** |

Open by category (final): language_count 148 · positioning 113 · free_claim 108 · region_count 82 ·
database_size 53 · other_mismatch 22 · regulation_count 1. AI-screen findings preserved: **214**.

**10-URL multi-fact spot check** (all distinct findings present):
- `/au/sds-management-articles/…` — 9 findings across database_size, free_claim, language_count, positioning, region_count
- `/us|uk|nz|eu|ca/about-us/` — 6 findings each (language_count, positioning, region_count)
- 4 more `/…/sds-management-articles/…` — 5 findings each (database_size + free_claim + …)

Distinct multi-fact URLs are **fully intact**; live CSV/XLSX exports equal DB open per category
(language_count 148, free_claim 108, database_size 53). No further action needed.
