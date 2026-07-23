"""Load fact rules from the DB (fact_rules table) in the shape the analysis
engine expects — so the automatic scan runs the rules users edit in the UI,
not the YAML file."""
from __future__ import annotations

import json


def _j(v):
    try:
        return json.loads(v) if v else []
    except (json.JSONDecodeError, TypeError):
        return []


def load_rules(conn, enabled_only: bool = True, product_id: int | None = None) -> list[dict]:
    """Load fact rules for the project. When `product_id` is given, return only that
    product's rules plus company-wide rules (product_id IS NULL); when None, every rule.
    A scan of the project checks ALL products' rules — product_id is for the UI filter."""
    q = ("SELECT fr.*, p.name AS product_name FROM fact_rules fr "
         "LEFT JOIN products p ON p.id = fr.product_id")
    conds, params = [], []
    if enabled_only:
        conds.append("fr.enabled=1")
    if product_id is not None:
        conds.append("(fr.product_id IS NULL OR fr.product_id=?)")
        params.append(product_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY fr.category, fr.slug"
    out = []
    for r in conn.execute(q, params):
        rt = r["rule_type"] or "stale"
        out.append({
            "id": r["slug"],
            "pk": r["id"],
            "product_id": r["product_id"],
            "product_name": r["product_name"],
            "category": r["category"],
            "type": rt,
            "description": r["description"] or r["slug"],
            "current_value": r["correct_value"],
            # inventory rules use canonical_value; reuse correct_value
            "canonical_value": r["correct_value"] if rt == "inventory" else None,
            "current_patterns": _j(r["current_patterns"]),
            "stale_patterns": _j(r["stale_patterns"]),
            "allowed_patterns": _j(r["allowed_patterns"]),
            "claim_patterns": _j(r["claim_patterns"]),
            "require_context": _j(r["require_context"]),
            "context_window": r["context_window"] or 120,
            "search_terms": _j(r["search_terms"]),
            "severity": r["severity"] or "medium",
            "applies_to": r["applies_to"] or "all",
        })
    return out


def load_features(conn, enabled_only: bool = True) -> list[dict]:
    q = "SELECT * FROM feature_entries"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY slug"
    return [{
        "id": r["slug"], "name": r["name"], "description": r["description"],
        "status": r["status"], "aliases": _j(r["aliases"]), "notes": r["notes"],
    } for r in conn.execute(q)]
