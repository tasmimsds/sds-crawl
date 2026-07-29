# Database sharing — how two of us should collaborate

## Why the contributor's DB was "null"
Nothing was broken. The database lives at `data/crawl.db` and is **intentionally
gitignored** (`data/`, `*.db`). So a `git clone` brings the **code** but not the
**data** — that's correct: SQLite files (100+ MB, constantly changing) must never
live in Git. On first run the app **auto-creates a valid but empty** `data/crawl.db`
with the full schema, so he got a working install with zero rows. The fix is to
share the DB out-of-band (the zip I made), not to commit it.

Verified:
- `data/crawl.db` and `*.db` are gitignored; the DB is not in git history.
- First run auto-creates all 26 tables + schema; empty DB is valid, not corrupt.
- My exported DB already carries the current schema (products, fact_matches,
  author/content_type, matched_value, dedup keys) and `connect()` migrations run
  cleanly and idempotently on load, so dropping my file onto his checkout just works.

## The real problem with passing a file around
A SQLite file is a **single-writer snapshot**. If we both crawl, we get two
divergent `crawl.db` files with no way to merge them — whoever sends their copy
last silently wins, and any findings the other person triaged are lost. Fine for a
one-time handoff (what the zip is for); unsustainable for ongoing collaboration.

---

## Option A — Shared hosted Postgres  ⭐ recommended for 2+ devs
Move the database to a managed Postgres (Supabase or Neon both have free tiers).
Both machines point at the **same** `DATABASE_URL` in their own local `.env`
(never committed). One source of truth: no file passing, no divergence, no
null-on-clone — a fresh clone just needs the URL and it's live.

What the migration involves (roughly a half-day, one-time):
- Introduce **SQLAlchemy** (or `psycopg`) as the engine and read `DATABASE_URL`
  from env; keep a SQLite fallback for offline/local use.
- Port the schema: our `_ensure_columns` migrations map cleanly; a few SQLite-isms
  need attention — `AUTOINCREMENT`→`SERIAL/IDENTITY`, and the **FTS5** full-text
  index (`content_fts`) has no Postgres equivalent, so swap it for Postgres
  `tsvector`/`pg_trgm` (or drop FTS and use `ILIKE`, since fact detection doesn't
  depend on it).
- One data migration: load this export into Postgres once (`.dump` → adapt → import,
  or a small copy script). Everything else — `llm_cache`, `app_settings`,
  `saved_filters`, products, fact rules — moves **as-is**; no behavior changes.
- Connection handling: swap the single `sqlite3.connect(check_same_thread=False)`
  for a pooled engine; the app is already request-scoped so this is contained.
- Cost: $0 on free tiers for our data size (~4 k URLs, well within limits).

Result: both devs crawl/triage against the same DB, live, forever. This is the
right long-term setup.

## Option B — Keep SQLite, share the file manually
Keep `data/crawl.db` local; share it via Google Drive / Dropbox when needed, and
**agree that only one person crawls at a time**. Simpler (no code change), but
manual and fragile: every sync is a full-file copy, and a forgotten "who's got the
latest?" means lost work. Acceptable as a stopgap; not a real multi-dev workflow.

## Either way
Keep the database **out of Git**. Confirmed `.gitignore` already covers
`data/`, `*.db`, `.env`, and `output/` (plus the export zips/dumps I just added).
Secrets stay in each dev's own `.env`, which is gitignored and never shared in-repo.

---

**Reply "do Option A" and I'll implement the Postgres migration.**
