# ExactFact Checker — by SDS Manager

**ExactFact Checker** is a standalone **fact-check & inspection tool** for **sdsmanager.com**
(and any site you add).
It does three things, and nothing else:

1. **Crawl** — sitemap auto-discovery from a root domain, with an Advanced Filter Builder to
   target exactly which URLs to fetch (locale, section, URL patterns, changed-since, date).
2. **Fact check** — internal (your pages vs your Facts Library) **and** external (what other
   sites claim about your brand). Every page-vs-fact evaluation is stored as
   **positive ✓ / issue ✗ / unclear ?**.
3. **Inspect & manage findings** — advanced filtering, match matrix, and
   recheck / edit / delete / false-positive learning.

There is **no** SEO, technical-SEO, canonical, cannibalization, or site-health functionality —
this tool is fact-checking only. (Basic fetch fields like HTTP status are kept internally just
to know a page is reachable before fact-checking; they are never surfaced as SEO issues.)

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

That's it. Findings live under **Results & Issues**; exports/reports are linked from there.

---

## What it checks (facts only)

- **Fact rules** (Facts Library, editable in the UI): database size (17M+), enterprise
  positioning (not "small business"), free-plan claims ("free trial" is fine), and
  language/region/translation/regulation counts (inventory + canonical value), scoped per
  **product** (SDS Manager / ExactSDS).
- **Query fact-search**: type any fact → AI expands it into search terms → full-text
  search → highlighted quotes → optional match/mismatch verdict.
- **Feature claims** vs your feature list; **FAQ** answers; site-wide **claim inventory**.
- **External fact check**: what other sites say about your brand, scoped to your brand/products.
- Every page-vs-fact evaluation is stored as **positive ✓ / issue ✗ / unclear ?** (match matrix).

_No SEO, technical-SEO, canonical, cannibalization, or site-health checks — removed by design._

## Navigation (left sidebar)

Dashboard · Fact Check · Results & Issues · Facts Library · Add Website · Settings.
The **website switcher** at the top scopes every page to the selected site.

## Synchronize Dashboard

- Per website: a **CRAWL → READ → FACT MATCH** pipeline flow with positive/issue/unclear
  counts, sync state, page counts, and last-synced time.
- **Sync Now** / **Sync Changes Only** / **Cancel** — one click runs crawl → read → fact
  match. The options step lets you pick which URLs to crawl (Advanced Filter Builder).
- **Auto-sync schedule**: Off / Daily / Weekly per site (runs a full sync automatically).
- **What changed** + **Recent sync activity** (click a row for run detail + error pages).

## CLI (optional, for power users)

```bash
.venv/bin/python -m src.cli migrate                 # set up / upgrade the DB
.venv/bin/python -m src.cli source add <url|file> --name "My site"
.venv/bin/python -m src.cli crawl <source>
.venv/bin/python -m src.cli analyze facts <source>  # regex + inventory + AI
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
