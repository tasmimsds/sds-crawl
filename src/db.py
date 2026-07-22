"""SQLite schema + helpers. Single file DB, standalone."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import resolve_path, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT,
    kind TEXT NOT NULL,               -- 'sitemap' | 'urllist' | 'root'
    location TEXT NOT NULL UNIQUE,    -- the sitemap url / file path / root url
    domain TEXT,
    created_at TEXT,
    last_run_at TEXT
);

CREATE TABLE IF NOT EXISTS urls (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    url TEXT UNIQUE NOT NULL,
    locale TEXT,
    section TEXT,
    lastmod TEXT,
    hreflang_group_id INTEGER,
    first_seen TEXT,
    last_crawled TEXT,
    in_source INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS crawl_results (
    id INTEGER PRIMARY KEY,
    url_id INTEGER REFERENCES urls(id),
    crawled_at TEXT,
    status_code INTEGER,
    final_url TEXT,
    redirect_chain TEXT,              -- JSON array
    response_time_ms INTEGER,
    error TEXT,
    content_hash TEXT,
    title TEXT,
    meta_description TEXT,
    canonical TEXT,
    meta_robots TEXT,
    h1 TEXT,                          -- JSON array
    h2s TEXT,                         -- JSON array
    word_count INTEGER,
    body_text TEXT
);

CREATE TABLE IF NOT EXISTS faqs (
    id INTEGER PRIMARY KEY,
    url_id INTEGER REFERENCES urls(id),
    question TEXT,
    answer TEXT,
    source TEXT                       -- 'visible' | 'jsonld'
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    url_id INTEGER REFERENCES urls(id),
    detected_at TEXT,
    category TEXT,                    -- see CATEGORIES
    severity TEXT,                    -- 'critical'|'high'|'medium'|'low'
    title TEXT,
    detail TEXT,
    evidence TEXT,                    -- exact quote (required for LLM findings)
    expected TEXT,
    related_url_id INTEGER DEFAULT 0, -- 0 = none (NULLs break UNIQUE dedupe)
    detection_method TEXT,            -- 'regex' | 'inventory' | 'llm' | 'crawl'
    status TEXT DEFAULT 'open',       -- 'open' | 'fixed' | 'ignored'
    query_id INTEGER,                 -- FK to queries(id) when found via query fact-search
    UNIQUE(url_id, category, title, related_url_id)
);

-- Fact rules (migrated from facts.yaml; end-user editable in the UI).
CREATE TABLE IF NOT EXISTS fact_rules (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE,                 -- original yaml id
    name TEXT,
    description TEXT,
    rule_type TEXT,                   -- 'stale' | 'inventory'
    category TEXT,
    correct_value TEXT,               -- current_value / canonical_value
    search_terms TEXT,                -- JSON array (for FTS/query expansion)
    current_patterns TEXT,            -- JSON array
    stale_patterns TEXT,              -- JSON array
    allowed_patterns TEXT,            -- JSON array
    claim_patterns TEXT,              -- JSON array (inventory rules)
    require_context TEXT,             -- JSON array
    context_window INTEGER DEFAULT 120,
    severity TEXT,
    applies_to TEXT DEFAULT 'all',
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

-- Feature entries (migrated from features.yaml; end-user editable in the UI).
CREATE TABLE IF NOT EXISTS feature_entries (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE,
    name TEXT,
    description TEXT,
    status TEXT,                      -- available | partial | not_available
    aliases TEXT,                     -- JSON array
    notes TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

-- Saved query-based fact searches + history.
CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    query_text TEXT,
    expanded_terms TEXT,              -- JSON array
    correct_value TEXT,
    category TEXT,
    created_at TEXT,
    last_run TEXT
);

-- Background jobs (crawl / scan) with live progress.
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    type TEXT,                        -- 'crawl' | 'sync' | 'scan' | 'query'
    source_id INTEGER REFERENCES sources(id),
    status TEXT DEFAULT 'queued',     -- queued|running|done|error|canceled
    stage TEXT,                       -- 'sitemap' | 'pages' | 'facts' | 'health'
    progress INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    cancelled INTEGER DEFAULT 0,
    urls_new INTEGER DEFAULT 0,
    urls_changed INTEGER DEFAULT 0,
    urls_removed INTEGER DEFAULT 0,
    issues_found INTEGER DEFAULT 0,
    issues_fixed INTEGER DEFAULT 0,
    message TEXT,
    error TEXT,
    created_at TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    content_hash TEXT,
    task TEXT,
    model TEXT,
    response TEXT,
    created_at TEXT,
    PRIMARY KEY (content_hash, task, model)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    started_at TEXT,
    finished_at TEXT,
    kind TEXT
);

-- Brand profile per project/source (scopes external findings to THIS brand).
CREATE TABLE IF NOT EXISTS brand_profiles (
    id INTEGER PRIMARY KEY,
    source_id INTEGER UNIQUE REFERENCES sources(id),
    brand_name TEXT,
    aliases TEXT,                     -- JSON array
    own_domains TEXT,                 -- JSON array (excluded from external crawling)
    disambiguation_notes TEXT,
    negative_terms TEXT,              -- JSON array (competitor names)
    updated_at TEXT
);

-- External pages (reviews, comparisons, forums…) — kept OUT of internal urls/crawl_results.
CREATE TABLE IF NOT EXISTS external_pages (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    url TEXT,
    domain TEXT,
    source_type TEXT,                 -- 'manual' | 'discovery'
    fetch_status TEXT DEFAULT 'pending',  -- pending|ok|blocked|error
    fetch_error TEXT,
    fetched_at TEXT,
    title TEXT,
    text TEXT,
    created_at TEXT,
    UNIQUE(source_id, url)
);
CREATE INDEX IF NOT EXISTS idx_extpage_source ON external_pages(source_id);

-- Brand-relevant passages extracted + scoped from external pages.
CREATE TABLE IF NOT EXISTS external_snippets (
    id INTEGER PRIMARY KEY,
    page_id INTEGER REFERENCES external_pages(id),
    source_id INTEGER REFERENCES sources(id),
    snippet TEXT,
    about_brand INTEGER DEFAULT 0,
    confidence REAL,
    reason TEXT,
    position INTEGER,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_extsnip_page ON external_snippets(page_id);

-- External fact-check findings (what other sites say about us, via SERP).
CREATE TABLE IF NOT EXISTS external_findings (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    kind TEXT,                        -- 'factcheck' | 'mention'
    fact_name TEXT,
    query TEXT,
    external_url TEXT,
    domain TEXT,
    title TEXT,
    snippet TEXT,
    verdict TEXT,                     -- mismatch | matches | unclear | (mention: sentiment)
    reason TEXT,
    expected TEXT,
    status TEXT DEFAULT 'open',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ext_source ON external_findings(source_id);

-- Per-website auto-sync schedule.
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    source_id INTEGER UNIQUE REFERENCES sources(id),
    mode TEXT DEFAULT 'off',          -- 'off' | 'daily' | 'weekly'
    day_of_week INTEGER DEFAULT 0,    -- 0=Mon .. 6=Sun (weekly)
    hour INTEGER DEFAULT 3,
    minute INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    next_run TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_urls_source ON urls(source_id);
CREATE INDEX IF NOT EXISTS idx_crawl_url ON crawl_results(url_id);
CREATE INDEX IF NOT EXISTS idx_faqs_url ON faqs(url_id);
CREATE INDEX IF NOT EXISTS idx_issues_source ON issues(source_id);
CREATE INDEX IF NOT EXISTS idx_issues_cat ON issues(category);
"""

CATEGORIES = [
    "status",
    "crawl",
    "seo_technical",
    "database_size",
    "positioning",
    "free_claim",
    "language_count",
    "region_count",
    "feature_claim",
    "faq",
    "cannibalization",
    "other_mismatch",
    "external_mismatch",
]

_conn: sqlite3.Connection | None = None
FTS_ENABLED = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    global _conn, FTS_ENABLED
    if _conn is not None:
        return _conn
    db_path = resolve_path(settings()["paths"]["db"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the web app touches this connection from both the
    # event loop and threadpool workers; WAL + busy_timeout keep it safe at this scale.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    # FTS5 full-text index over crawled content (fast keyword layer).
    try:
        conn.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(
                 url_id UNINDEXED, url UNINDEXED, title, meta_description, h1, faqs, body_text,
                 tokenize='porter unicode61'
               )"""
        )
        FTS_ENABLED = True
    except sqlite3.OperationalError:
        FTS_ENABLED = False  # fallback to LIKE search
    _ensure_columns(conn, "jobs", {
        "stage": "TEXT", "cancelled": "INTEGER DEFAULT 0",
        "urls_new": "INTEGER DEFAULT 0", "urls_changed": "INTEGER DEFAULT 0",
        "urls_removed": "INTEGER DEFAULT 0", "issues_found": "INTEGER DEFAULT 0",
        "issues_fixed": "INTEGER DEFAULT 0",
    })
    _ensure_columns(conn, "fact_rules", {"scope": "TEXT DEFAULT 'both'"})
    _ensure_columns(conn, "external_findings", {
        "finding_type": "TEXT", "fact_rule": "TEXT", "page_id": "INTEGER",
        "last_checked_at": "TEXT", "severity": "TEXT DEFAULT 'high'",
        "deleted_at": "TEXT", "note": "TEXT",
    })
    _ensure_columns(conn, "issues", {
        "deleted_at": "TEXT", "note": "TEXT", "edited": "INTEGER DEFAULT 0",
        "original_snapshot": "TEXT", "last_checked_at": "TEXT",
    })
    conn.commit()
    _conn = conn
    return conn


def _ensure_columns(conn, table: str, cols: dict[str, str]) -> None:
    """Idempotently add missing columns to an existing table."""
    have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def fts_index_url(conn, url_id: int) -> None:
    """(Re)index one URL's latest crawl content + FAQs into content_fts."""
    if not FTS_ENABLED:
        return
    import json as _json

    row = conn.execute(
        """SELECT u.url, c.title, c.meta_description, c.h1, c.body_text
           FROM urls u JOIN crawl_results c ON c.id=(
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1)
           WHERE u.id=?""",
        (url_id,),
    ).fetchone()
    conn.execute("DELETE FROM content_fts WHERE url_id=?", (url_id,))
    if not row or not row["body_text"]:
        return
    h1 = " ".join(_json.loads(row["h1"])) if row["h1"] else ""
    faqs = " ".join(
        f"{r['question']} {r['answer']}"
        for r in conn.execute("SELECT question, answer FROM faqs WHERE url_id=?", (url_id,))
    )
    conn.execute(
        """INSERT INTO content_fts (url_id, url, title, meta_description, h1, faqs, body_text)
           VALUES (?,?,?,?,?,?,?)""",
        (url_id, row["url"], row["title"] or "", row["meta_description"] or "", h1, faqs,
         row["body_text"]),
    )


# ---- sources -------------------------------------------------------------

def add_source(conn, kind: str, location: str, name: str | None, domain: str | None) -> int:
    conn.execute(
        """INSERT INTO sources (name, kind, location, domain, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(location) DO UPDATE SET name=COALESCE(excluded.name, sources.name)""",
        (name, kind, location, domain, now_iso()),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM sources WHERE location=?", (location,)).fetchone()
    return row["id"]


def resolve_source(conn, ref: str):
    """Resolve a source by id, name, or location string."""
    if ref.isdigit():
        row = conn.execute("SELECT * FROM sources WHERE id=?", (int(ref),)).fetchone()
        if row:
            return row
    row = conn.execute(
        "SELECT * FROM sources WHERE name=? OR location=?", (ref, ref)
    ).fetchone()
    return row


def list_sources(conn):
    return conn.execute("SELECT * FROM sources ORDER BY id").fetchall()


# ---- urls ----------------------------------------------------------------

def upsert_url(conn, source_id, url, locale, section, lastmod) -> int:
    conn.execute(
        """INSERT INTO urls (source_id, url, locale, section, lastmod, first_seen, in_source)
           VALUES (?, ?, ?, ?, ?, ?, 1)
           ON CONFLICT(url) DO UPDATE SET
             locale=excluded.locale, section=excluded.section,
             lastmod=excluded.lastmod, in_source=1""",
        (source_id, url, locale, section, lastmod, now_iso()),
    )
    row = conn.execute("SELECT id FROM urls WHERE url=?", (url,)).fetchone()
    return row["id"]


def set_hreflang_group(conn, url_id: int, group_id: int) -> None:
    conn.execute("UPDATE urls SET hreflang_group_id=? WHERE id=?", (group_id, url_id))


# ---- issues --------------------------------------------------------------

def record_issue(
    conn,
    *,
    source_id: int,
    url_id: int,
    category: str,
    severity: str,
    title: str,
    detail: str | None = None,
    evidence: str | None = None,
    expected: str | None = None,
    related_url_id: int = 0,
    detection_method: str = "regex",
) -> None:
    conn.execute(
        """INSERT INTO issues
             (source_id, url_id, detected_at, category, severity, title, detail,
              evidence, expected, related_url_id, detection_method, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
           ON CONFLICT(url_id, category, title, related_url_id) DO UPDATE SET
             detected_at=excluded.detected_at, severity=excluded.severity,
             detail=excluded.detail, evidence=excluded.evidence,
             expected=excluded.expected, detection_method=excluded.detection_method,
             -- keep user decisions; a soft-deleted finding stays suppressed unless its
             -- evidence text changes (then it's a new finding and reopens).
             status=CASE
                 WHEN issues.status IN ('ignored','false_positive') THEN issues.status
                 WHEN issues.deleted_at IS NOT NULL AND issues.evidence=excluded.evidence THEN issues.status
                 ELSE 'open' END,
             deleted_at=CASE
                 WHEN issues.deleted_at IS NOT NULL AND issues.evidence!=excluded.evidence THEN NULL
                 ELSE issues.deleted_at END""",
        (
            source_id, url_id, now_iso(), category, severity, title, detail,
            evidence, expected, related_url_id or 0, detection_method,
        ),
    )


def reconcile_fixed(conn, source_id: int, categories: list[str], run_start_iso: str) -> int:
    """Diff mode: open issues in these categories not re-detected this run -> fixed."""
    if not categories:
        return 0
    marks = ",".join("?" for _ in categories)
    cur = conn.execute(
        f"""UPDATE issues SET status='fixed'
            WHERE status='open' AND source_id=? AND detected_at < ?
              AND category IN ({marks})""",
        (source_id, run_start_iso, *categories),
    )
    conn.commit()
    return cur.rowcount


# ---- FAQs ----------------------------------------------------------------

def clear_faqs(conn, url_id: int) -> None:
    conn.execute("DELETE FROM faqs WHERE url_id=?", (url_id,))


def add_faq(conn, url_id: int, question: str, answer: str, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO faqs (url_id, question, answer, source) VALUES (?, ?, ?, ?)",
        (url_id, question, answer, source),
    )
    return cur.lastrowid


# ---- LLM cache -----------------------------------------------------------

def get_cached_llm(conn, content_hash: str, task: str, model: str) -> str | None:
    row = conn.execute(
        "SELECT response FROM llm_cache WHERE content_hash=? AND task=? AND model=?",
        (content_hash, task, model),
    ).fetchone()
    return row["response"] if row else None


def put_cached_llm(conn, content_hash: str, task: str, model: str, response: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO llm_cache (content_hash, task, model, response, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (content_hash, task, model, response, now_iso()),
    )
    conn.commit()


# ---- runs ----------------------------------------------------------------

def start_run(conn, source_id: int, kind: str) -> tuple[int, str]:
    started = now_iso()
    cur = conn.execute(
        "INSERT INTO runs (source_id, started_at, kind) VALUES (?, ?, ?)",
        (source_id, started, kind),
    )
    conn.execute("UPDATE sources SET last_run_at=? WHERE id=?", (started, source_id))
    conn.commit()
    return cur.lastrowid, started


def finish_run(conn, run_id: int) -> None:
    conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (now_iso(), run_id))
    conn.commit()


def source_urls(conn, source_id: int, only_in_source: bool = True):
    q = "SELECT * FROM urls WHERE source_id=?"
    if only_in_source:
        q += " AND in_source=1"
    q += " ORDER BY id"
    return conn.execute(q, (source_id,)).fetchall()


def latest_crawl_join(conn, source_id: int):
    """Latest crawl_result per URL for a source, joined with the url row."""
    return conn.execute(
        """SELECT u.id AS url_id, u.url, u.locale, u.section, u.hreflang_group_id,
                  c.status_code, c.final_url, c.redirect_chain, c.response_time_ms, c.error,
                  c.content_hash, c.title, c.meta_description, c.canonical, c.meta_robots,
                  c.h1, c.h2s, c.word_count, c.body_text
           FROM urls u
           JOIN crawl_results c ON c.id = (
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1
           )
           WHERE u.source_id=?""",
        (source_id,),
    ).fetchall()
