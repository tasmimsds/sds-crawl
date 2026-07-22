# MIGRATION PLAN — Reposition to a Fact-Checking product with a Web UI

Status: **awaiting approval** — no product code will be written until this is approved.

Repositioning in one line: **Fact Check becomes the core engine and home screen**
(internal now, external later); **Site Health** (technical SEO, cannibalization,
crawl health) is demoted to a secondary section; and a **web UI** replaces the
CLI-first experience for non-technical users. CLI stays for power use.

---

## 1. What exists now (audited)

**Stack:** Python 3.12 (venv), async `httpx` crawler, `selectolax` parser, SQLite,
`typer` CLI, `jinja2` (one static report), OpenRouter via `openai` SDK. ~2,490 LOC.

**Modules (`src/`):**
| File | LOC | Role | State |
|---|---|---|---|
| `crawler.py` | 222 | async crawl, redirects, retries, 429/503 backoff, stores crawl_results + FAQs | solid, reuse |
| `extractor.py` | 158 | HTML→content + FAQ (visible + FAQPage JSON-LD) | solid, reuse |
| `ingest.py` | 285 | source add: sitemap / URL-list / root discovery + hreflang groups | solid, reuse |
| `db.py` | 310 | schema + helpers (issues dedupe, llm_cache, runs, reconcile) | reuse + extend |
| `config.py` | 44 | loads settings/facts/features YAML + `.env` | extend (DB-backed facts/features) |
| `util.py` | 104 | locale parse, hashing, DSU, context | reuse |
| `analysis/llm.py` | 79 | OpenRouter client, JSON-mode, cache, call cap, `max_tokens` | reuse |
| `analysis/fact_check.py` | 117 | 2-stage LLM fact/positioning/free/counts | **restructure → core** |
| `analysis/facts.py` | 88 | regex stale-pattern engine | **restructure → DB rules** |
| `analysis/inventory.py` | 135 | site-wide claim inventory + consistency | **restructure → core** |
| `analysis/features.py` | 69 | LLM feature-claim check | restructure → DB entries |
| `analysis/faqs.py` | 92 | LLM FAQ check | reuse (core) |
| `analysis/cannibalization.py` | 155 | TF-IDF + LLM verdict | **demote → Site Health** |
| `analysis/technical.py` | 111 | status/redirect/canonical/thin/etc. | **demote → Site Health** |
| `analysis/external.py` | 39 | documented stub | keep/extend, do not implement |
| `report/html_report.py` | 91 | static single-file jinja dashboard | keep for export; UI supersedes |
| `report/csv_export.py` | 48 | per-category CSVs | reuse for export |
| `cli.py` | 282 | typer entry (15 commands) | keep working; UI added alongside |

**DB tables (live data — must be preserved):**
`sources`(1) · `urls`(4,200) · `crawl_results`(4,047) · `faqs`(4,151) ·
`issues`(1,445) · `llm_cache`(2,905) · `runs`(0).

**Config:** `settings.yaml` (crawl/LLM/thresholds — stays a file),
`facts.yaml` (5 fact rules → migrate to DB), `features.yaml` (40 entries → migrate to DB).

**LLM state:** models configurable per-run (`--fast-model`/`--reasoning-model` + env).
Current default = free tier (`gemma-4-26b-a4b-it:free` + `nemotron-3-super-120b-a12b:free`)
because the OpenRouter key's $10/mo cap is exhausted; free models work at $0. Query
expansion + verdict passes will use whatever tier is configured.

---

## 2. Reused as-is (should mostly survive untouched)

- `crawler.py` (+ politeness/backoff/hreflang), `extractor.py` (+ FAQ), `ingest.py`
  (3 input modes + discovery), `util.py`.
- `analysis/llm.py` — OpenRouter two-tier client, JSON-mode, content-hash cache,
  call cap, `max_tokens` guard.
- Guardrails, unchanged: detection/report only (never modify live pages), only crawl
  explicitly-added sources, evidence-quote requirement (no quote → no issue), issue
  diffing / auto-mark-fixed, secrets in `.env` only.
- `issues` table shape (dedupe key, severity, status) — extended, not reshaped.
- `report/csv_export.py` and the HTML report generator (become "export" buttons).

---

## 3. Restructured (fact-checking becomes the core service)

- **New package `src/factcheck/`** as the product core, composed of the existing
  analysis pieces (not rewritten):
  - `query.py` — **new headline feature.** Query-based fact search:
    (1) FTS5 keyword retrieval → (2) LLM query-expansion (fast_model, editable terms)
    → (3) snippet extraction + highlight → (4) optional verdict pass
    (reasoning_model, ambiguous only) → issues with `query_id`.
  - `scan.py` — automatic fact scan: orchestrates existing regex rules + inventory +
    feature + FAQ checks against the latest crawl (wraps today's `facts.py` /
    `inventory.py` / `fact_check.py` / `features.py` / `faqs.py`, now reading rules
    from DB instead of YAML). Two-stage design + cache kept exactly.
- **YAML → DB:** `config.facts()` / `config.features()` become DB-backed. A one-time
  import migrates `facts.yaml` (both `stale` and `inventory` rule types) and
  `features.yaml` into new tables; YAML import/export retained for backup.
- Analysis modules change only their **rule source** (DB rows instead of YAML dicts);
  their detection logic is untouched.

---

## 4. Added (new capabilities)

- **Web UI** — `src/app.py` (FastAPI) + `templates/` (Jinja2 + HTMX). **All CSS/JS
  vendored locally** under `static/` (no CDN — works offline / in locked-down
  networks, no runtime external calls). Minimal vanilla JS for tabs (no Alpine).
  Single process: `python -m src.app` → localhost. No Node build step.
  **Deploy scaffolding included:** `Dockerfile` + `.dockerignore` + `Procfile` /
  `uvicorn` entry so it runs with one command on any VPS / Render / Railway / Fly.io.
  Pages: **Fact Check** (home: query box + correct-value + Internal/External tabs,
  saved rules with pass/fail, query history) · **Results** (highlighted snippets,
  filters, group-by-hreflang, Mark Fixed/Ignore/Open) · **Facts Library** (CRUD for
  rules + features) · **Site Health** (demoted technical + cannibalization) · **Reports**
  (HTML/CSV/Fact-Check-summary export).
- **Onboarding wizard** — one field (URL / sitemap / file upload), auto-detects type,
  reuses `ingest.py`. User never needs to know what a sitemap is.
- **Background jobs** — `jobs` table + in-process asyncio task; crawl runs with a live
  progress bar (done/total, errors) via HTMX polling. "Re-check site" = one click
  (`--only-changed`).
- **Search index** — SQLite **FTS5** virtual table over `body_text` + title + meta +
  h1 + FAQ text, kept in sync on crawl write; backfill for existing 4,017 pages.
- **New tables:** `fact_rules`, `feature_entries`, `queries` (saved query history),
  `jobs`. `issues` gains nullable `query_id` FK.
- **DB migration script** — adds tables + FTS5, backfills FTS from existing crawl data,
  imports current `facts.yaml`/`features.yaml`, `ALTER TABLE issues ADD query_id`.
  Idempotent; preserves all existing rows.

New Python deps: `fastapi`, `uvicorn[standard]`, `python-multipart` (uploads).
Frontend libs vendored under `static/` (no CDN, no build step). FTS5 availability
will be verified first (standard in Homebrew Python's sqlite; fallback = LIKE-based
search if absent).

---

## 5. Demoted, NOT deleted → "Site Health"

- `analysis/technical.py` (status/redirect/noindex/canonical/title/meta/H1/thin/slow)
  and `analysis/cannibalization.py` keep working and keep writing to `issues`; they're
  simply grouped under a secondary **Site Health** nav section and given plain-language
  labels in the UI. No logic removed.

---

## 6. External Fact Check — stub only (per instruction)

Second UI tab renders a clean "coming soon" state. `external.py` interface extended and
documented (input: claim/fact + external source list → output: same issue shape). **No
logic built** until requirements arrive.

---

## 7. Proposed build order (matches your spec; smoke-test in browser each step)

1. **DB migrations** — new tables + FTS5 + YAML→DB import; existing data preserved. ✔ verify counts unchanged.
2. **FastAPI skeleton + jobs + onboarding wizard + background crawl w/ progress.**
3. **Query fact search end-to-end** — box → expansion (editable) → FTS → highlighted results (no verdicts yet).
4. **Verdict pass + "save as fact rule" + issues integration** (query_id).
5. **Facts Library UI + automatic fact scan wired to DB rules.**
6. **Site Health + Reports pages** (surface existing analyses; exports).
7. **Polish** — empty states, error handling, plain-language copy, External stub tab.

Final: `README.md` gets a non-technical quickstart (install → one command → open
localhost → add website → first fact check).

---

## 8. Decisions — CONFIRMED

- ✅ **Reuse existing crawl data** — no re-crawl; FTS5 backfills from the 4,017
  already-crawled pages. "Re-check site" (one-click re-crawl) added for later use.
- ✅ **Keep `technical.py` + `cannibalization.py`** — demoted to Site Health, not deleted.
- ✅ **Stack:** FastAPI + Jinja2 + HTMX, assets **vendored locally** (no CDN).
- ✅ **Deploy scaffolding now:** Dockerfile + PaaS config; local run stays `python -m src.app`.
- ✅ **`settings.yaml` stays a file** (operator tuning); only `facts`/`features` → DB (end-user editable).
- **LLM:** builds/tests against the currently-active **free** models (key at $0); works
  today. Query use bounded by existing call cap + content-hash cache. Swappable to the
  paid combo anytime via flags once the key limit is raised.
- **No data loss:** migration is additive and idempotent; `data/crawl.db` is backed up
  to `data/crawl.db.bak` before any schema change.

**FTS5 present?** Verified as the first action in Phase 1; LIKE fallback if absent
(unlikely on this Python).
