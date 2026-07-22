"""Run saved query-type fact rules during a sync: FTS retrieve -> verdict -> issues."""
from __future__ import annotations

from ..db import record_issue
from ..rules import load_rules
from .query import _MARK, search, verdict


async def run_query_rules(conn, source_id: int) -> int:
    """Execute enabled query-type rules against the latest crawl; record mismatches."""
    # load_rules() exposes the correct value as "current_value"
    rules = [r for r in load_rules(conn) if r["type"] == "query" and r.get("current_value")]
    made = 0
    for r in rules:
        correct = r["current_value"]
        terms = r.get("search_terms") or [r.get("description") or r["id"]]
        result = search(conn, source_id, terms)
        if not result["rows"]:
            continue
        await verdict(conn, result["rows"], correct)
        for row in result["rows"]:
            if row.get("verdict") != "mismatch" or not row.get("url_id"):
                continue
            record_issue(
                conn, source_id=source_id, url_id=row["url_id"],
                category=r["category"] or "other_mismatch", severity=r["severity"],
                title=f"rule:{r['id']}",
                detail=row.get("verdict_reason") or f"Contradicts expected value: {correct}",
                evidence=_MARK.sub("", row["snippet"]), expected=correct,
                detection_method="llm",
            )
            made += 1
    conn.commit()
    return made
