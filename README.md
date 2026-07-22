# SDS Fact Check — Crawling & Analyzing System

A standalone internal QA tool for **sdsmanager.com**. Its #1 job is **fact-checking**:
crawl every page, then find **wrong / outdated / inconsistent claims** and show them
as fixable issues — each with the page URL, the exact quote, and what it should say.
Technical SEO checks and cannibalization live under a secondary **Site Health** section.

It's **web-first** (a browser app anyone can use — no commands) with a CLI for power users.
Detection & reporting only — it never modifies your live site.

---

## Quickstart (non-technical)

**1. One-time setup** (in a terminal, in this folder):
```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
Optional: to enable the AI checks, put an OpenRouter key in a file named `.env`:
```
OPENROUTER_API_KEY=your-key-here
```
(The regex + inventory checks work with no key. Free AI models also work at $0.)

**2. Start the app** (one command):
```bash
.venv/bin/python -m src.app
```
Then open **http://localhost:8000** in your browser.

**3. Add your website.** On first run you'll see "Add your website." Paste your
website address (e.g. `https://yourcompany.com`) **or** a sitemap link, **or** upload
a `.txt`/`.csv` list of URLs. Click **Add website**. You don't need to know what a
sitemap is — the app figures it out.

**4. Sync it.** On the **Dashboard**, click **Sync Now** on your website's card.
Watch the progress bar (it checks the sitemap, then the pages, then the facts).

**5. Fact-check.** Go to **Fact Check** and type a question, e.g.:
- `SDS database size` with correct value `17 million+`
- `free plan or free option claims`
- `how many languages do we support`

You'll get every matching page with the exact quote highlighted, and (if you gave a
correct value) a ✓ match / ✗ mismatch verdict. Click **＋ Save as fact rule** to have
it run automatically on every future sync.

That's it. Findings live under **Results & Issues**; technical problems under
**Site Health**; exports under **Reports**.

---

## What it checks

- **Fact rules** (Facts Library, editable in the UI): database size (17M+), enterprise
  positioning (not "small business"), free-plan claims ("free trial" is fine), and
  language/region/translation counts (inventory + canonical value).
- **Query fact-search**: type any fact → AI expands it into search terms → full-text
  search → highlighted quotes → optional match/mismatch verdict.
- **Feature claims** vs your feature list; **FAQ** answers; site-wide **claim inventory**.
- **Site Health**: 4xx/5xx, redirects, noindex, canonical, missing/duplicate title·meta·H1,
  thin content, slow pages, and within-locale **cannibalization**.

## Navigation (left sidebar)

Dashboard · Fact Check · Results & Issues · Facts Library · Site Health · Reports ·
Add Website · Settings. The **website switcher** at the top scopes every page to the
selected site.

## Synchronize Dashboard

- One card per website: **Synced ✓ / Needs sync / Syncing… / Failed**, page counts,
  new/changed/errors, last-synced time.
- **Sync Now** / **Sync Changes Only** / **Cancel** — one click runs crawl → fact
  rules → technical checks (no manual steps).
- **Auto-sync schedule**: Off / Daily / Weekly per site (runs a full sync automatically).
- **What changed** + **Recent sync activity** (click a row for run detail + error pages).

## CLI (optional, for power users)

```bash
.venv/bin/python -m src.cli migrate                 # set up / upgrade the DB
.venv/bin/python -m src.cli source add <url|file> --name "My site"
.venv/bin/python -m src.cli crawl <source>
.venv/bin/python -m src.cli analyze facts <source>  # regex + inventory + AI
.venv/bin/python -m src.cli analyze technical <source>
.venv/bin/python -m src.cli inventory claims <source>
.venv/bin/python -m src.cli report html <source>
.venv/bin/python -m src.cli run-all <source>
.venv/bin/python -m src.cli models                  # show/override AI models
```
`<source>` is a source id, name, or URL. Override AI models per run with
`--fast-model` / `--reasoning-model`.

## Configuration

- **Facts Library** (in the UI) — add/edit fact rules and features; **Export/Import YAML**
  for backup. This is the source of truth the scan uses.
- `config/settings.yaml` — crawl concurrency/timeouts, AI model names, LLM call cap,
  similarity threshold. `config/facts.yaml` / `features.yaml` are the initial seed
  (imported into the DB on `migrate`).
- `.env` — `OPENROUTER_API_KEY` (+ optional `OPENROUTER_APP_URL` / `_TITLE`).

## Deployment

Single process, no Node build. A `Dockerfile` + PaaS config can be added for hosting;
locally, `python -m src.app` is all you need. Data lives in `data/crawl.db`
(SQLite + FTS5); reports/exports in `output/`. Both are gitignored.

## Guardrails

Detection/report only (never edits live pages) · only crawls sources you add · backs
off on 429/503 · every AI finding carries the exact evidence quote · secrets only in
`.env`.
