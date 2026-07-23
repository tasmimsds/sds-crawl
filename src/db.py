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

-- Products within a project (one company/domain can sell several products).
-- project_id references the main site's sources.id.
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES sources(id),
    name TEXT NOT NULL,
    aliases TEXT,                     -- JSON array
    notes TEXT,
    is_default INTEGER DEFAULT 0,     -- the main/default product for the project
    created_at TEXT,
    UNIQUE(project_id, name)
);

-- Saved advanced filters / crawl scopes (per project, per context).
CREATE TABLE IF NOT EXISTS saved_filters (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    context TEXT,                     -- 'findings' | 'crawl'
    name TEXT,
    model TEXT,                       -- JSON filter model
    created_at TEXT,
    UNIQUE(source_id, context, name)
);

-- App-wide settings edited in the UI (the single source of truth for model config).
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

-- Every fact-vs-page evaluation (positive / issue / unclear). Issues also live in
-- `issues` for the workflow; fact_matches is the complete verdict log for the matrix.
CREATE TABLE IF NOT EXISTS fact_matches (
    id INTEGER PRIMARY KEY,
    fact_rule_id INTEGER REFERENCES fact_rules(id),
    url_id INTEGER DEFAULT 0,          -- internal page (0 if external)
    external_page_id INTEGER DEFAULT 0,-- external page (0 if internal)
    verdict TEXT,                      -- 'positive' | 'issue' | 'unclear'
    evidence TEXT,                     -- the exact quote from the page
    matched_value TEXT,                -- the value found on the page
    product_id INTEGER,
    checked_at TEXT,
    run_id INTEGER,
    UNIQUE(fact_rule_id, url_id, external_page_id)
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
    "database_size",
    "positioning",
    "free_claim",
    "language_count",
    "region_count",
    "regulation_count",
    "feature_claim",
    "faq",
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
        # pipeline-flow counts (READ + MATCH stages)
        "pages_read": "INTEGER DEFAULT 0", "pages_unreadable": "INTEGER DEFAULT 0",
        "claims_extracted": "INTEGER DEFAULT 0", "faqs_extracted": "INTEGER DEFAULT 0",
        "facts_checked": "INTEGER DEFAULT 0", "matches_positive": "INTEGER DEFAULT 0",
        "matches_issue": "INTEGER DEFAULT 0", "matches_unclear": "INTEGER DEFAULT 0",
    })
    _ensure_columns(conn, "fact_rules", {"scope": "TEXT DEFAULT 'both'", "product_id": "INTEGER"})
    _ensure_columns(conn, "issues", {"product_id": "INTEGER"})
    _ensure_columns(conn, "sources", {"run_scope": "TEXT", "locale_scope": "TEXT"})
    _ensure_columns(conn, "jobs", {"scope_label": "TEXT", "run_scope": "TEXT"})
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
    _seed_settings(conn)  # premium model defaults + migrate away any stored :free model
    # Fact-check-only cleanup: purge legacy SEO/technical/cannibalization findings (idempotent).
    conn.execute("DELETE FROM issues WHERE category IN "
                 "('seo_technical','cannibalization','status','crawl')")
    conn.commit()
    _conn = conn
    return conn


# ---- run scope (analysis + locale) persisted per project ----
ENGLISH_PRESET = ["us", "uk", "eu", "au", "ca", "nz"]
# Recommended default: Fact Check only (+ FAQ), English locales — cheapest.
DEFAULT_RUN_SCOPE = {"fact_check": 1, "faq": 1}  # fact-check only tool


def project_delete_summary(conn, source_id: int) -> dict:
    """Counts of everything a project delete would remove (for the confirm dialog)."""
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]
    prod_ids = [r["id"] for r in conn.execute("SELECT id FROM products WHERE project_id=?", (source_id,))]
    rules = 0
    if prod_ids:
        marks = ",".join("?" * len(prod_ids))
        rules = conn.execute(f"SELECT COUNT(*) FROM fact_rules WHERE product_id IN ({marks})",
                             prod_ids).fetchone()[0]
    src = conn.execute("SELECT name, location FROM sources WHERE id=?", (source_id,)).fetchone()
    return {
        "name": (src["name"] or src["location"]) if src else str(source_id),
        "pages": one("SELECT COUNT(*) FROM urls WHERE source_id=?", source_id),
        "matches": one("SELECT COUNT(*) FROM fact_matches WHERE url_id IN "
                       "(SELECT id FROM urls WHERE source_id=?)", source_id),
        "findings": one("SELECT COUNT(*) FROM issues WHERE source_id=?", source_id)
                    + one("SELECT COUNT(*) FROM external_findings WHERE source_id=?", source_id),
        "rules": rules,
        "external": one("SELECT COUNT(*) FROM external_pages WHERE source_id=?", source_id),
        "jobs": one("SELECT COUNT(*) FROM jobs WHERE source_id=?", source_id),
        "schedules": one("SELECT COUNT(*) FROM schedules WHERE source_id=?", source_id),
    }


def delete_project(conn, source_id: int) -> dict:
    """Permanently delete a project and every dependent row, in one transaction.
    Returns an orphan-check dict (all values must be 0). Company-wide fact rules
    (product_id IS NULL) are shared and NOT deleted."""
    url_ids = [r["id"] for r in conn.execute("SELECT id FROM urls WHERE source_id=?", (source_id,))]
    prod_ids = [r["id"] for r in conn.execute("SELECT id FROM products WHERE project_id=?", (source_id,))]
    try:
        conn.execute("BEGIN")
        if url_ids:
            m = ",".join("?" * len(url_ids))
            conn.execute(f"DELETE FROM fact_matches WHERE url_id IN ({m})", url_ids)
            conn.execute(f"DELETE FROM faqs WHERE url_id IN ({m})", url_ids)
            conn.execute(f"DELETE FROM crawl_results WHERE url_id IN ({m})", url_ids)
            if FTS_ENABLED:
                try:
                    conn.execute(f"DELETE FROM content_fts WHERE url_id IN ({m})", url_ids)
                except sqlite3.OperationalError:
                    pass
        for t in ("issues", "external_findings", "external_snippets", "external_pages",
                  "brand_profiles", "queries", "jobs", "schedules"):
            try:
                conn.execute(f"DELETE FROM {t} WHERE source_id=?", (source_id,))
            except sqlite3.OperationalError:
                pass
        if prod_ids:
            m = ",".join("?" * len(prod_ids))
            conn.execute(f"DELETE FROM fact_matches WHERE fact_rule_id IN "
                         f"(SELECT id FROM fact_rules WHERE product_id IN ({m}))", prod_ids)
            conn.execute(f"DELETE FROM fact_rules WHERE product_id IN ({m})", prod_ids)
        conn.execute("DELETE FROM products WHERE project_id=?", (source_id,))
        conn.execute("DELETE FROM urls WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    # orphan verification
    orphans = {
        "sources": conn.execute("SELECT COUNT(*) FROM sources WHERE id=?", (source_id,)).fetchone()[0],
        "urls": conn.execute("SELECT COUNT(*) FROM urls WHERE source_id=?", (source_id,)).fetchone()[0],
        "issues": conn.execute("SELECT COUNT(*) FROM issues WHERE source_id=?", (source_id,)).fetchone()[0],
        "jobs": conn.execute("SELECT COUNT(*) FROM jobs WHERE source_id=?", (source_id,)).fetchone()[0],
        "products": conn.execute("SELECT COUNT(*) FROM products WHERE project_id=?", (source_id,)).fetchone()[0],
        "crawl_results": conn.execute("SELECT COUNT(*) FROM crawl_results WHERE url_id NOT IN "
                                      "(SELECT id FROM urls)", ()).fetchone()[0] if url_ids else 0,
    }
    return orphans


def source_domain(conn, source_id: int) -> str | None:
    """The project's OWN registrable domain (per-project, not the global default).
    This is the domain the crawler is allowed to fetch for THIS project."""
    from .util import registrable_domain
    r = conn.execute("SELECT domain, location FROM sources WHERE id=?", (source_id,)).fetchone()
    if not r:
        return None
    return r["domain"] or registrable_domain(r["location"])


def locales_for_source(conn, source_id: int) -> list[dict]:
    """Detected locales in the project's crawlable URLs, with counts. Root/no-locale
    pages are grouped as '(root)'. Ordered by count desc."""
    primary = source_domain(conn, source_id) or ""
    rows = conn.execute(
        "SELECT COALESCE(locale,'(root)') loc, COUNT(*) n FROM urls "
        "WHERE source_id=? AND in_source=1 AND url LIKE ? GROUP BY COALESCE(locale,'(root)') "
        "ORDER BY (loc='(root)'), n DESC", (source_id, f"%{primary}%")).fetchall()
    return [{"code": r["loc"], "count": r["n"]} for r in rows]


def has_locale_structure(conn, source_id: int) -> bool:
    return any(l["code"] != "(root)" for l in locales_for_source(conn, source_id))


def get_run_config(conn, source_id: int) -> dict:
    """Saved analysis scope + locale scope for a project (recommended defaults if unset)."""
    import json as _json
    r = conn.execute("SELECT run_scope, locale_scope FROM sources WHERE id=?", (source_id,)).fetchone()
    scope = dict(DEFAULT_RUN_SCOPE)
    locale = {"mode": "all", "locales": []}
    if r:
        try:
            if r["run_scope"]:
                scope.update(_json.loads(r["run_scope"]))
        except (ValueError, TypeError):
            pass
        try:
            if r["locale_scope"]:
                locale = _json.loads(r["locale_scope"])
        except (ValueError, TypeError):
            pass
    return {"scope": scope, "locale": locale}


def set_run_config(conn, source_id: int, scope: dict, locale: dict) -> None:
    import json as _json
    conn.execute("UPDATE sources SET run_scope=?, locale_scope=? WHERE id=?",
                 (_json.dumps(scope), _json.dumps(locale), source_id))
    conn.commit()


def scope_label(scope: dict, locale: dict) -> str:
    """Human label for a run's coverage, e.g. 'English locales · Fact Check'."""
    mode = (locale or {}).get("mode", "all")
    loc = {"all": "All locales", "english": "English locales", "custom": "Custom locales",
           "advanced": "Advanced scope"}.get(mode, "All locales")
    return f"{loc} · Fact Check"


def get_products(conn, project_id: int) -> list[dict]:
    """All products defined for a project (main site)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM products WHERE project_id=? ORDER BY is_default DESC, name", (project_id,))]


def default_product_id(conn, project_id: int) -> int | None:
    r = conn.execute(
        "SELECT id FROM products WHERE project_id=? ORDER BY is_default DESC, id LIMIT 1",
        (project_id,)).fetchone()
    return r["id"] if r else None


def product_brand_aliases(conn, project_id: int) -> list[str]:
    """Every product name + alias for a project — these all count as brand aliases
    so external pages mentioning any product are scoped in under the project."""
    import json as _json
    out = []
    for p in get_products(conn, project_id):
        if p["name"]:
            out.append(p["name"])
        try:
            out.extend(a for a in _json.loads(p["aliases"] or "[]") if a)
        except (ValueError, TypeError):
            pass
    # de-dupe, preserve order
    seen, uniq = set(), []
    for a in out:
        if a.lower() not in seen:
            seen.add(a.lower()); uniq.append(a)
    return uniq


def match_product(conn, project_id: int, text: str) -> int | None:
    """Return the product_id whose name/alias appears in `text`, else None.
    Used to auto-assign a product to an interpreted fact — never creates a project."""
    import json as _json
    if not text:
        return None
    t = text.lower()
    best = None
    for p in get_products(conn, project_id):
        if p["is_default"]:  # default matches last (only if nothing more specific hit)
            continue
        names = [p["name"]] + (_json.loads(p["aliases"] or "[]") if p["aliases"] else [])
        if any(n and n.lower() in t for n in names):
            return p["id"]
    return best


def brand_for_source(conn, source_id, default: str = "SDS Manager") -> str:
    """The brand/product display name for a site (used in LLM prompts)."""
    if not source_id:
        return default
    r = conn.execute("SELECT brand_name FROM brand_profiles WHERE source_id=?", (source_id,)).fetchone()
    return r["brand_name"] if r and r["brand_name"] else default


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
    status: str = "open",
    product_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO issues
             (source_id, url_id, detected_at, category, severity, title, detail,
              evidence, expected, related_url_id, detection_method, product_id, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url_id, category, title, related_url_id) DO UPDATE SET
             detected_at=excluded.detected_at, severity=excluded.severity,
             detail=excluded.detail, evidence=excluded.evidence,
             expected=excluded.expected, detection_method=excluded.detection_method,
             product_id=excluded.product_id,
             -- keep user decisions; a soft-deleted finding stays suppressed unless its
             -- evidence text changes (then it's a new finding and reopens).
             status=CASE
                 WHEN issues.status IN ('ignored','false_positive') THEN issues.status
                 WHEN issues.deleted_at IS NOT NULL AND issues.evidence=excluded.evidence THEN issues.status
                 ELSE excluded.status END,
             deleted_at=CASE
                 WHEN issues.deleted_at IS NOT NULL AND issues.evidence!=excluded.evidence THEN NULL
                 ELSE issues.deleted_at END""",
        (
            source_id, url_id, now_iso(), category, severity, title, detail,
            evidence, expected, related_url_id or 0, detection_method, product_id, status,
        ),
    )


def record_match(conn, *, fact_rule_id, verdict: str, url_id: int = 0,
                 external_page_id: int = 0, evidence: str | None = None,
                 matched_value: str | None = None, product_id: int | None = None,
                 run_id: int | None = None) -> None:
    """Persist a fact-vs-page verdict (positive/issue/unclear). Upserts per (fact,page)."""
    if not fact_rule_id:
        return
    conn.execute(
        """INSERT INTO fact_matches
             (fact_rule_id, url_id, external_page_id, verdict, evidence, matched_value,
              product_id, checked_at, run_id)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(fact_rule_id, url_id, external_page_id) DO UPDATE SET
             verdict=excluded.verdict, evidence=excluded.evidence,
             matched_value=excluded.matched_value, product_id=excluded.product_id,
             checked_at=excluded.checked_at, run_id=excluded.run_id""",
        (fact_rule_id, url_id or 0, external_page_id or 0, verdict, evidence,
         matched_value, product_id, now_iso(), run_id),
    )


def clear_matches_for_source(conn, source_id: int) -> None:
    """Drop internal fact_matches for a source before a full re-scan (fresh counts)."""
    conn.execute("DELETE FROM fact_matches WHERE url_id IN "
                 "(SELECT id FROM urls WHERE source_id=?)", (source_id,))


def rule_pk(conn, slug: str) -> int | None:
    r = conn.execute("SELECT id FROM fact_rules WHERE slug=?", (slug,)).fetchone()
    return r["id"] if r else None


# ---- app settings (single source of truth for model config) ----
def get_setting(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] is not None else default


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), now_iso()))
    conn.commit()


def get_model_config(conn) -> dict:
    """The live model configuration. DB app_settings is authoritative; premium code
    defaults are the fallback. Free models are never returned as a default."""
    from .config import DEFAULT_FAST_MODEL, DEFAULT_REASONING_MODEL, DEFAULT_SPEND_CAP_USD
    fast = get_setting(conn, "fast_model", DEFAULT_FAST_MODEL)
    reasoning = get_setting(conn, "reasoning_model", DEFAULT_REASONING_MODEL)
    return {
        "fast_model": fast,
        "reasoning_model": reasoning,
        "interpret_model": get_setting(conn, "interpret_model", reasoning),
        "spend_cap_usd": float(get_setting(conn, "spend_cap_usd", DEFAULT_SPEND_CAP_USD)),
    }


def _seed_settings(conn) -> None:
    """Seed premium model defaults on first run, and MIGRATE any stored free model
    away to premium (so existing installs are fixed, not just fresh ones)."""
    from .config import (DEFAULT_FAST_MODEL, DEFAULT_REASONING_MODEL,
                         DEFAULT_REASONING_MODEL as _INTERP, DEFAULT_SPEND_CAP_USD)
    seeds = {"fast_model": DEFAULT_FAST_MODEL, "reasoning_model": DEFAULT_REASONING_MODEL,
             "interpret_model": _INTERP, "spend_cap_usd": DEFAULT_SPEND_CAP_USD}
    for k, v in seeds.items():
        cur = get_setting(conn, k)
        # seed if missing; migrate if a free model somehow got stored
        if cur is None or (k.endswith("_model") and ":free" in (cur or "")):
            set_setting(conn, k, v)
    # validate stored model ids against the live catalog; remap a changed id to the
    # closest current version of the same family (card labels stay the same).
    try:
        from .openrouter import validate_id
        for k in ("fast_model", "reasoning_model", "interpret_model"):
            cur = get_setting(conn, k)
            if cur:
                resolved, remapped = validate_id(cur)
                if remapped and resolved != cur:
                    set_setting(conn, k, resolved)
    except Exception:  # noqa: BLE001 — never let validation block startup (offline/no key)
        pass


def reconcile_fixed(conn, source_id: int, categories: list[str], run_start_iso: str,
                    methods: list[str] | None = None) -> int:
    """Diff mode: open issues NOT re-detected this run -> fixed.

    `methods` restricts reconciliation to issues produced by the detectors that actually
    ran this run (by detection_method). This prevents wrongly marking a detector's issues
    'fixed' when that detector didn't run (e.g. the AI screening pass was skipped/paused)."""
    if not categories:
        return 0
    marks = ",".join("?" for _ in categories)
    where = ["status='open'", "source_id=?", "detected_at < ?", f"category IN ({marks})"]
    params = [source_id, run_start_iso, *categories]
    if methods:
        where.append(f"detection_method IN ({','.join('?' for _ in methods)})")
        params += methods
    cur = conn.execute(f"UPDATE issues SET status='fixed' WHERE {' AND '.join(where)}", params)
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
