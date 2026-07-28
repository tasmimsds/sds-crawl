"""Advanced Filter Builder — compile a JSON group-tree into safe parameterized SQL.

Model shape (from the reusable UI builder):
    {
      "scopes": {"internal": true, "external": false, "positive": false, ...},
      "groups": [
        {"mode": "all|any|none", "field": "severity", "values": ["high", "critical"]},
        ...
      ]
    }

Per group: mode All=AND / Any=OR / None=NOT within the group's value chips.
Between groups (left→right): Any→OR, All→AND, None→AND (the group already carries its NOT),
reproducing the reference `(A) OR (B OR C) NOT (D)`. Fields are whitelisted per context and
values are always bound as parameters — the client never sends SQL.
"""
from __future__ import annotations

import json

from .db import now_iso

# field -> (sql column, kind). kind drives how a value chip becomes a condition.
FINDINGS_FIELDS = {
    "category": ("i.category", "enum"),
    "severity": ("i.severity", "enum"),
    "status": ("i.status", "enum"),
    "locale": ("u.locale", "enum"),
    "content_type": ("u.content_type", "enum"),
    "author": ("u.author", "contains"),
    "product": ("i.product_id", "product"),
    "fact_rule": ("i.title", "contains"),
    "url_contains": ("u.url", "contains"),
    "evidence_contains": ("i.evidence", "contains"),
    "date_detected": ("i.detected_at", "date"),
}
EXTERNAL_FIELDS = {
    "category": ("ef.finding_type", "enum"),
    "severity": ("ef.severity", "enum"),
    "status": ("ef.status", "enum"),
    "source_domain": ("ef.domain", "enum"),
    "url_contains": ("ef.external_url", "contains"),
    "evidence_contains": ("ef.snippet", "contains"),
    "date_detected": ("ef.created_at", "date"),
}
CRAWL_FIELDS = {
    "locale": ("u.locale", "enum"),
    "section": ("u.section", "enum"),
    "content_type": ("u.content_type", "enum"),
    "url_contains": ("u.url", "contains"),
    "url_not_contains": ("u.url", "not_contains"),
    "lastmod": ("u.lastmod", "date"),
    "changed": (None, "changed"),  # special: changed-since-last-sync
}


def _cond(col, kind, v, params) -> str | None:
    v = str(v).strip()
    if not v:
        return None
    if kind == "enum":
        params.append(v); return f"{col}=?"
    if kind == "product":
        params.append(v); return f"{col} IN (SELECT id FROM products WHERE name=?)"
    if kind == "contains":
        params.append(f"%{v}%"); return f"{col} LIKE ?"
    if kind == "not_contains":
        params.append(f"%{v}%"); return f"{col} NOT LIKE ?"
    if kind == "changed":
        # v in {"yes","no"}; "yes" = lastmod newer than last crawl (or never crawled)
        yes = "(u.last_crawled IS NULL OR (u.lastmod IS NOT NULL AND u.lastmod>u.last_crawled))"
        return yes if v.lower() in ("yes", "true", "1") else f"NOT {yes}"
    if kind == "date":
        start, _, end = v.partition("|")
        parts = []
        if start.strip():
            params.append(start.strip()); parts.append(f"{col}>=?")
        if end.strip():
            params.append(end.strip() + "T23:59:59"); parts.append(f"{col}<=?")
        return "(" + " AND ".join(parts) + ")" if parts else None
    return None


def _group_sql(g, fmap, params):
    field = g.get("field")
    spec = fmap.get(field)
    if not spec:
        return None
    col, kind = spec
    mode = (g.get("mode") or "any").lower()
    conds = []
    for v in g.get("values", []):
        c = _cond(col, kind, v, params)
        if c:
            conds.append(c)
    if not conds:
        return None
    join = " AND " if mode == "all" else " OR "
    sub = "(" + join.join(conds) + ")"
    if mode == "none":
        sub = "NOT " + sub
    return sub, mode


def compile_model(model: dict, fmap: dict) -> tuple[str, list]:
    """Return (where_sql, params). Empty where ('') = no constraint. Groups whose field
    isn't in this context's field map are skipped (so one model works across tables)."""
    params: list = []
    where = None
    for g in (model or {}).get("groups", []):
        r = _group_sql(g, fmap, params)
        if not r:
            continue
        sub, mode = r
        if where is None:
            where = sub
        else:
            op = "OR" if mode == "any" else "AND"
            where = f"({where}) {op} {sub}"
    return (where or ""), params


def scopes_of(model: dict) -> dict:
    return (model or {}).get("scopes") or {}


# ---- saved filters (per project, per context) ----
def save_filter(conn, source_id, context, name, model) -> None:
    conn.execute(
        "INSERT INTO saved_filters(source_id,context,name,model,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(source_id,context,name) DO UPDATE SET model=excluded.model",
        (source_id, context, name, json.dumps(model), now_iso()))
    conn.commit()


def list_filters(conn, source_id, context) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "model": json.loads(r["model"] or "{}")}
            for r in conn.execute(
                "SELECT id,name,model FROM saved_filters WHERE source_id=? AND context=? ORDER BY name",
                (source_id, context))]


def get_filter(conn, filter_id) -> dict | None:
    r = conn.execute("SELECT model FROM saved_filters WHERE id=?", (filter_id,)).fetchone()
    return json.loads(r["model"]) if r else None


def delete_filter(conn, filter_id) -> None:
    conn.execute("DELETE FROM saved_filters WHERE id=?", (filter_id,))
    conn.commit()
