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


def load_rules(conn, enabled_only: bool = True) -> list[dict]:
    q = "SELECT * FROM fact_rules"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY category, slug"
    out = []
    for r in conn.execute(q):
        rt = r["rule_type"] or "stale"
        out.append({
            "id": r["slug"],
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
