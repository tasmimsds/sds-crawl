"""Unified CSV export — ONE generator for every issue/findings download.

Everything (web live-download, /results filtered export, CLI file dump) goes through
`build_issue_csv` so columns, scope semantics, UTF-8-BOM, and escaping can never drift.
Scope: 'open' (default, == the UI) or 'all' (every non-deleted status). Rows are already
deduped per (url, fact_rule) by record_issue's ON CONFLICT.
"""
from __future__ import annotations

import csv
import io

from ..config import resolve_path, settings
from ..db import CATEGORIES

# consistent columns for every issue CSV
COLS = ["url", "locale", "product", "category", "severity", "status", "fact_rule",
        "evidence", "expected", "detection_method", "detected_at"]
_BOM = "﻿"  # so Excel opens Japanese/Hindi/Greek/German evidence without mojibake


def _issue_rows(conn, source_id, category=None, scope="open"):
    where = ["i.source_id=?", "i.deleted_at IS NULL"]
    params = [source_id]
    if category:
        where.append("i.category=?")
        params.append(category)
    if scope == "open":
        where.append("i.status='open'")
    return conn.execute(
        f"""SELECT u.url, u.locale, pp.name AS product, i.category, i.severity, i.status,
                  i.title AS fact_rule, i.evidence, i.expected, i.detection_method, i.detected_at
           FROM issues i JOIN urls u ON u.id=i.url_id
           LEFT JOIN products pp ON pp.id=i.product_id
           WHERE {' AND '.join(where)}
           ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 ELSE 3 END, u.url""",
        params,
    ).fetchall()


def build_issue_csv(conn, source_id: int, category: str | None = None, scope: str = "open") -> str:
    """Live CSV text (UTF-8 BOM). category=None => all fact categories combined."""
    buf = io.StringIO()
    w = csv.writer(buf)  # csv module handles quotes/commas/newlines in evidence safely
    w.writerow([f"# scope={scope}  category={category or 'all'}  (generated live from DB)"])
    w.writerow(COLS)
    for r in _issue_rows(conn, source_id, category, scope):
        w.writerow([r["url"], r["locale"] or "", r["product"] or "", r["category"], r["severity"],
                    r["status"], r["fact_rule"] or "", r["evidence"] or "", r["expected"] or "",
                    r["detection_method"] or "", r["detected_at"] or ""])
    return _BOM + buf.getvalue()


def build_external_csv(conn, source_id: int, scope: str = "open") -> str:
    where = ["source_id=?", "kind='factcheck'", "deleted_at IS NULL"]
    params = [source_id]
    if scope == "open":
        where.append("status='open'")
    rows = conn.execute(
        f"""SELECT domain, external_url, finding_type, severity, status, snippet, expected, reason, created_at
            FROM external_findings WHERE {' AND '.join(where)} ORDER BY domain""", params).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# external findings  scope={scope}  (generated live from DB)"])
    w.writerow(["domain", "external_url", "finding_type", "severity", "status", "evidence",
                "expected", "reason", "found_at"])
    for r in rows:
        w.writerow([r["domain"] or "", r["external_url"], r["finding_type"] or "", r["severity"] or "",
                    r["status"], r["snippet"] or "", r["expected"] or "", r["reason"] or "", r["created_at"] or ""])
    return _BOM + buf.getvalue()


def build_rows_csv(rows, ext_rows) -> str:
    """CSV for the Results advanced-filter view (already-fetched row dicts), same columns+BOM."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["scope", *COLS])
    for r in rows:
        w.writerow(["internal", r["url"], r.get("locale") or "", r.get("product_name") or "",
                    r["category"], r["severity"], r["status"], r.get("title") or "",
                    r.get("evidence") or "", r.get("expected") or "", r.get("detection_method") or "",
                    r.get("last_checked_at") or ""])
    for r in ext_rows:
        w.writerow(["external", r["external_url"], "", "", r.get("finding_type") or "", r["severity"],
                    r["status"], "", r.get("snippet") or "", r.get("expected") or "", "", ""])
    return _BOM + buf.getvalue()


def export_csv(conn, source_id: int, *, scope: str = "open"):
    """CLI/file dump: one file per category via the shared generator (UTF-8 BOM)."""
    out_dir = resolve_path(settings()["paths"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for cat in CATEGORIES:
        rows = _issue_rows(conn, source_id, cat, scope)
        if not rows:
            continue
        path = out_dir / f"issues_{cat}_{scope}.csv"
        path.write_text(build_issue_csv(conn, source_id, cat, scope), encoding="utf-8")
        files.append(str(path))
        print(f"  wrote {len(rows)} rows -> {path}")
    ext = conn.execute("SELECT COUNT(*) c FROM external_findings WHERE source_id=? AND kind='factcheck'",
                       (source_id,)).fetchone()["c"]
    if ext:
        path = out_dir / "external_findings.csv"
        path.write_text(build_external_csv(conn, source_id, scope), encoding="utf-8")
        files.append(str(path))
    if not files:
        print("No issues to export.")
    return files
