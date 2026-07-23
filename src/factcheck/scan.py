"""Run saved query-type fact rules during a sync: FTS retrieve -> verdict -> issues."""
from __future__ import annotations

from ..db import record_issue, record_match
from ..rules import load_rules
from .query import _MARK, search, verdict


async def run_query_rules(conn, source_id: int) -> int:
    """Execute enabled query-type rules against the latest crawl; record mismatches."""
    # load_rules() exposes the correct value as "current_value"
    rules = [r for r in load_rules(conn)
             if r["type"] == "query" and r.get("current_value")]
    made = 0
    for r in rules:
        correct = r["current_value"]
        terms = r.get("search_terms") or [r.get("description") or r["id"]]
        result = search(conn, source_id, terms)
        if not result["rows"]:
            continue
        await verdict(conn, result["rows"], correct)
        for row in result["rows"]:
            v = row.get("verdict")
            uid = row.get("url_id")
            if not uid:
                continue
            ev = _MARK.sub("", row["snippet"])
            if v == "mismatch":
                record_issue(
                    conn, source_id=source_id, url_id=uid,
                    category=r["category"] or "other_mismatch", severity=r["severity"],
                    title=f"rule:{r['id']}",
                    detail=row.get("verdict_reason") or f"Contradicts expected value: {correct}",
                    evidence=ev, expected=correct,
                    detection_method="llm", product_id=r.get("product_id"),
                )
                record_match(conn, fact_rule_id=r.get("pk"), url_id=uid, verdict="issue",
                             evidence=ev, matched_value=correct, product_id=r.get("product_id"))
                made += 1
            elif v == "matches":
                record_match(conn, fact_rule_id=r.get("pk"), url_id=uid, verdict="positive",
                             evidence=ev, matched_value=correct, product_id=r.get("product_id"))
            elif v == "unclear":
                record_match(conn, fact_rule_id=r.get("pk"), url_id=uid, verdict="unclear",
                             evidence=ev, product_id=r.get("product_id"))
    conn.commit()
    return made
