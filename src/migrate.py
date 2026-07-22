"""One-time (idempotent) migration to the fact-checking product data model.

- adds new tables (via connect()/SCHEMA): fact_rules, feature_entries, queries, jobs
- adds issues.query_id to existing DBs
- builds + backfills the FTS5 index from existing crawl data (no re-crawl)
- imports config/facts.yaml + config/features.yaml into the new tables

Safe to run repeatedly. Back up data/crawl.db before first run.
"""
from __future__ import annotations

import json
import shutil

from . import db
from .config import facts as load_facts, features as load_features, resolve_path, settings
from .db import connect, fts_index_url, now_iso


def _has_column(conn, table: str, col: str) -> bool:
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _backup_db() -> str | None:
    db = resolve_path(settings()["paths"]["db"])
    if db.exists():
        bak = db.with_suffix(".db.bak")
        shutil.copy2(db, bak)
        return str(bak)
    return None


def _add_issue_query_id(conn) -> bool:
    if _has_column(conn, "issues", "query_id"):
        return False
    conn.execute("ALTER TABLE issues ADD COLUMN query_id INTEGER")
    conn.commit()
    return True


def _backfill_fts(conn) -> int:
    if not db.FTS_ENABLED:
        print("  ! FTS5 not available — skipping index backfill (LIKE fallback will be used)")
        return 0
    conn.execute("DELETE FROM content_fts")
    url_ids = [
        r["id"]
        for r in conn.execute(
            """SELECT u.id FROM urls u
               WHERE EXISTS (SELECT 1 FROM crawl_results c WHERE c.url_id=u.id)"""
        )
    ]
    n = 0
    for uid in url_ids:
        fts_index_url(conn, uid)
        n += 1
    conn.commit()
    return n


def _import_facts(conn) -> int:
    now = now_iso()
    rows = 0
    for f in load_facts():
        conn.execute(
            """INSERT INTO fact_rules
                 (slug, name, description, rule_type, category, correct_value, search_terms,
                  current_patterns, stale_patterns, allowed_patterns, claim_patterns,
                  require_context, context_window, severity, applies_to, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 name=excluded.name, description=excluded.description, rule_type=excluded.rule_type,
                 category=excluded.category, correct_value=excluded.correct_value,
                 current_patterns=excluded.current_patterns, stale_patterns=excluded.stale_patterns,
                 allowed_patterns=excluded.allowed_patterns, claim_patterns=excluded.claim_patterns,
                 require_context=excluded.require_context, context_window=excluded.context_window,
                 severity=excluded.severity, applies_to=excluded.applies_to, updated_at=excluded.updated_at""",
            (
                f["id"], f.get("description", f["id"]), f.get("description"),
                f.get("type", "stale"), f.get("category"),
                f.get("current_value") or f.get("canonical_value"),
                json.dumps(f.get("search_terms", [])),
                json.dumps(f.get("current_patterns", [])),
                json.dumps(f.get("stale_patterns", [])),
                json.dumps(f.get("allowed_patterns", [])),
                json.dumps(f.get("claim_patterns", [])),
                json.dumps(f.get("require_context", [])),
                f.get("context_window", 120), f.get("severity", "medium"),
                f.get("applies_to", "all"), now, now,
            ),
        )
        rows += 1
    conn.commit()
    return rows


def _import_features(conn) -> int:
    now = now_iso()
    rows = 0
    for ft in load_features():
        conn.execute(
            """INSERT INTO feature_entries
                 (slug, name, description, status, aliases, notes, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,1,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 name=excluded.name, description=excluded.description, status=excluded.status,
                 aliases=excluded.aliases, notes=excluded.notes, updated_at=excluded.updated_at""",
            (
                ft["id"], ft.get("name", ft["id"]), ft.get("description"),
                ft.get("status", "available"), json.dumps(ft.get("aliases", [])),
                ft.get("notes"), now, now,
            ),
        )
        rows += 1
    conn.commit()
    return rows


def migrate(backup: bool = True) -> dict:
    if backup:
        bak = _backup_db()
        if bak:
            print(f"Backup: {bak}")
    conn = connect()  # creates new tables + FTS virtual table
    print(f"FTS5 enabled: {db.FTS_ENABLED}")
    added = _add_issue_query_id(conn)
    print(f"issues.query_id: {'added' if added else 'already present'}")
    facts_n = _import_facts(conn)
    features_n = _import_features(conn)
    print(f"Imported {facts_n} fact rules, {features_n} feature entries from YAML.")
    fts_n = _backfill_fts(conn)
    print(f"FTS backfill: indexed {fts_n} pages.")
    return {"query_id_added": added, "facts": facts_n, "features": features_n, "fts_pages": fts_n}
