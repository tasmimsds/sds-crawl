# Author + content_type for blog/news pages

Every finding now carries its page's `content_type` (blog / news / other) and, for blog & news
pages only, the `author`. Everything else (landing, product, pricing, root) is `other` with a
**blank** author — never a guessed one.

## Content type (URL path, region-agnostic)
`util.content_type_of(url)` — segment match, ignoring the region segment (`/us/`, `/uk/`, `/au/`…):
- path has `sds-management-articles` → **blog**
- path has `chemical-hse-news` → **news**
- otherwise → **other**

## Author extraction (blog/news only) — `extractor._extract_author`
First strategy that yields a name wins:
1. **JSON-LD** `Article`/`NewsArticle`/`BlogPosting` → `author.name` (object or array; multiple
   joined with `; `). Walks `@graph`/`mainEntity`/`hasPart`.
2. **Meta tags**: `article:author`, `og:article:author`, `meta[name=author]`, `meta[property=author]`
   (profile-URL values skipped).
3. **Visible byline**: `rel="author"`, `.author`/`.byline`/`.writer`/`[class*=author]`/`[itemprop=author]`,
   plus a free-text `By {Name}` / `Written by {Name}` / `Author: {Name}` fallback in the article body.

Cleaning: strip the `By`/`Written by`/`Author:` prefix and any trailing date/role after a
separator (`|`, `•`, `,`, `—`, ` on `) — e.g. `By Jane Doe, Senior Editor — March 2024` → `Jane Doe`,
`By Samiha Audrika | 19 Mar 2026` → `Samiha Audrika`.

`author_status`: `found` / `not_found` (blog/news with no byline) / `not_applicable` (other).

## Storage — page-level on `urls`
New columns `urls.content_type`, `urls.author`, `urls.author_status`. Written by the crawler
(`_store` now receives the URL) and available to every finding on the page via the existing
`issues → urls` join.

## Exports & UI
- **CSV + Excel** (every category, filtered view, by-URL, full report): new `content_type` and
  `author` columns placed right after `url`/`locale`. Non-blog/news author cells are empty.
- **Results & Issues UI**: a `blog`/`news` pill and a `✍ Author` chip on each finding that has one.
- **HTML report**: content-type + author tags on each row.
- **Advanced filter**: `content_type` (blog/news/other, enum) and `author` (contains) added to
  `FINDINGS_FIELDS`; `content_type` also added to `CRAWL_FIELDS`. So "all issues on news pages" or
  "all findings by {author}" are filterable.

## Backfill (path used: TARGETED RE-FETCH)
Raw HTML is not stored (only extracted text), so authors required a fetch:
- **content_type**: pure path parse for **all 4206 URLs**, instant, in `db.backfill_content_type`
  (runs in `connect()`, idempotent) → **1569 blog · 39 news · 2598 other**.
- **author**: `crawler.backfill_authors` re-fetched **only the 1608 blog/news URLs** with the normal
  politeness (concurrency 8, 0.25 s/worker delay, throttle on 429/503); updates `urls.author` in
  place, does not rewrite `crawl_results`. Result: **1608 found · 0 not_found · 0 errors**.

## Verification (all pass)
- `/au/sds-management-articles/5-principles-…` → **blog**, author **Zarif Ahmed**.
- `/eu/chemical-hse-news/ensuring-safe-chemical-transport-…` → **news**, author **Samiha Audrika**.
- `/at/pricing/` (and `/at/`) → **other**, author blank, `not_applicable`.
- CSV & XLSX headers both include `content_type` + `author`; blog rows show the author; all `other`
  rows are genuinely empty (not "none").
- `content_type=news` filter → 22 findings, all news; author filter returns only that author's rows.
- UI: blog filter shows 176 findings, each with a `blog` pill and `✍ author` chip.
