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
COLS = ["url", "locale", "content_type", "author", "product", "category", "severity", "status",
        "fact_rule", "evidence", "matched_value", "expected", "detection_method", "detected_at"]
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
        f"""SELECT u.url, u.locale, u.content_type, u.author, pp.name AS product, i.category,
                  i.severity, i.status, i.title AS fact_rule, i.evidence, i.matched_value,
                  i.expected, i.detection_method, i.detected_at
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
        w.writerow([r["url"], r["locale"] or "", r["content_type"] or "", r["author"] or "",
                    r["product"] or "", r["category"], r["severity"], r["status"],
                    r["fact_rule"] or "", r["evidence"] or "", r["matched_value"] or "",
                    r["expected"] or "", r["detection_method"] or "", r["detected_at"] or ""])
    return _BOM + buf.getvalue()


EXT_COLS = ["domain", "external_url", "finding_type", "severity", "status", "evidence",
            "expected", "reason", "found_at"]


def external_rows(conn, source_id, scope="open"):
    """Shared external-findings rows (dicts keyed by EXT_COLS) — used by CSV and XLSX."""
    where = ["source_id=?", "kind='factcheck'", "deleted_at IS NULL"]
    params = [source_id]
    if scope == "open":
        where.append("status='open'")
    rows = conn.execute(
        f"""SELECT domain, external_url, finding_type, severity, status, snippet, expected, reason, created_at
            FROM external_findings WHERE {' AND '.join(where)} ORDER BY domain""", params).fetchall()
    return [{"domain": r["domain"] or "", "external_url": r["external_url"],
             "finding_type": r["finding_type"] or "", "severity": r["severity"] or "",
             "status": r["status"], "evidence": r["snippet"] or "", "expected": r["expected"] or "",
             "reason": r["reason"] or "", "found_at": r["created_at"] or ""} for r in rows]


def build_external_csv(conn, source_id: int, scope: str = "open") -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# external findings  scope={scope}  (generated live from DB)"])
    w.writerow(EXT_COLS)
    for r in external_rows(conn, source_id, scope):
        w.writerow([r[c] for c in EXT_COLS])
    return _BOM + buf.getvalue()


URL_COLS = ["url", "locale", "content_type", "author", "product", "issue_count", "categories",
            "severities", "evidences"]


def url_summary_rows(conn, source_id, scope="open"):
    """One row per URL, aggregating its distinct findings (nothing discarded).
    issue_count/categories/evidences summarise all findings on that page."""
    from collections import OrderedDict
    agg: "OrderedDict" = OrderedDict()
    for r in _issue_rows(conn, source_id, None, scope):
        a = agg.setdefault(r["url"], {"url": r["url"], "locale": r["locale"] or "",
                                      "content_type": r["content_type"] or "", "author": r["author"] or "",
                                      "product": r["product"] or "", "cats": [], "sevs": set(), "evs": []})
        a["cats"].append(r["category"])
        a["sevs"].add(r["severity"])
        if r["evidence"]:
            a["evs"].append(r["evidence"])
    order = ["critical", "high", "medium", "low"]
    out = []
    for a in agg.values():
        top = next((s for s in order if s in a["sevs"]), "")
        out.append({"url": a["url"], "locale": a["locale"], "content_type": a["content_type"],
                    "author": a["author"], "product": a["product"],
                    "issue_count": len(a["cats"]), "categories": "; ".join(sorted(set(a["cats"]))),
                    "severities": top, "evidences": " | ".join(e[:120] for e in a["evs"][:10])})
    out.sort(key=lambda x: -x["issue_count"])
    return out


def build_url_summary_csv(conn, source_id: int, scope: str = "open") -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# one row per URL (aggregated)  scope={scope}  (generated live from DB)"])
    w.writerow(URL_COLS)
    for r in url_summary_rows(conn, source_id, scope):
        w.writerow([r[c] for c in URL_COLS])
    return _BOM + buf.getvalue()


EXTITEM_COLS = ["source_url", "domain", "mention_type", "anchor_text", "context_paragraph",
                "brand_mentions_found", "matched_fact", "verdict", "needs_change"]


def external_item_rows(conn, source_id, aliases=None):
    """Backlink/mention items with mention_type, anchor, paragraph, verdict — the
    link+mention context report. Only the requested fields (no backlink-metric dump)."""
    aliases = [a.lower() for a in (aliases or []) if a]
    rows = conn.execute(
        """SELECT p.url AS source_url, p.domain, p.mention_type, p.anchor_text,
                  p.context_paragraph, p.fetch_status,
                  (SELECT f.fact_rule FROM external_findings f WHERE f.page_id=p.id
                     AND f.kind='factcheck' AND f.status='open' AND f.deleted_at IS NULL LIMIT 1) AS matched_fact,
                  (SELECT 1 FROM external_findings f WHERE f.page_id=p.id AND f.kind='factcheck'
                     AND f.status='open' AND f.deleted_at IS NULL LIMIT 1) AS is_mismatch,
                  (SELECT g.needs_change FROM general_facts g WHERE g.page_id=p.id AND g.status='open' LIMIT 1) AS needs_change,
                  (SELECT 1 FROM general_facts g WHERE g.page_id=p.id AND g.status='open' LIMIT 1) AS is_general
           FROM external_pages p
           WHERE p.source_id=? AND p.source_type IN ('backlink','mention')
           ORDER BY p.mention_type, p.id DESC""", (source_id,)).fetchall()
    out = []
    for r in rows:
        para = (r["context_paragraph"] or "").lower()
        found = "; ".join(sorted({a for a in aliases if a in para})) if aliases else ""
        verdict = ("mismatch" if r["is_mismatch"] else "general" if r["is_general"]
                   else "discarded" if r["fetch_status"] == "ok" else "pending")
        out.append({"source_url": r["source_url"], "domain": r["domain"],
                    "mention_type": r["mention_type"] or "", "anchor_text": r["anchor_text"] or "",
                    "context_paragraph": r["context_paragraph"] or "",
                    "brand_mentions_found": found, "matched_fact": r["matched_fact"] or "",
                    "verdict": verdict, "needs_change": r["needs_change"] or ""})
    return out


def build_external_items_csv(conn, source_id: int, aliases=None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# external backlinks & mentions (link/mention context report)"])
    w.writerow(EXTITEM_COLS)
    for r in external_item_rows(conn, source_id, aliases):
        w.writerow([r[c] for c in EXTITEM_COLS])
    return _BOM + buf.getvalue()


GENFACT_COLS = ["quote", "domain", "source_url", "source_kind", "needs_change", "note",
                "status", "created_at"]


def general_fact_rows(conn, source_id, scope="open"):
    where = ["source_id=?"]
    params = [source_id]
    if scope == "open":
        where.append("status='open'")
    return conn.execute(
        f"""SELECT quote, domain, source_url, source_kind, needs_change, note, status, created_at
            FROM general_facts WHERE {' AND '.join(where)} ORDER BY id DESC""", params).fetchall()


def build_general_facts_csv(conn, source_id: int, scope: str = "open") -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# general facts (brand info not mapped to a defined fact)  scope={scope}"])
    w.writerow(GENFACT_COLS)
    for r in general_fact_rows(conn, source_id, scope):
        w.writerow([r[c] or "" for c in GENFACT_COLS])
    return _BOM + buf.getvalue()


def build_rows_csv(rows, ext_rows) -> str:
    """CSV for the Results advanced-filter view (already-fetched row dicts), same columns+BOM."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["scope", *COLS])
    for r in rows:
        w.writerow(["internal", r["url"], r.get("locale") or "", r.get("content_type") or "",
                    r.get("author") or "", r.get("product_name") or "",
                    r["category"], r["severity"], r["status"], r.get("title") or "",
                    r.get("evidence") or "", r.get("matched_value") or "", r.get("expected") or "",
                    r.get("detection_method") or "", r.get("last_checked_at") or ""])
    for r in ext_rows:
        w.writerow(["external", r["external_url"], "", "", "", "", r.get("finding_type") or "",
                    r["severity"], r["status"], "", r.get("snippet") or "", "",
                    r.get("expected") or "", "", ""])
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
