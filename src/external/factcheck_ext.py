"""External fact checking — run Facts Library rules (scope external/both) against
brand-scoped external snippets; write external_findings with a finding_type."""
from __future__ import annotations

from ..db import now_iso
from ..factcheck.query import verdict
from ..rules import load_rules

# map fact category -> external finding_type
_FT = {
    "database_size": "outdated_info", "language_count": "outdated_info",
    "region_count": "outdated_info", "positioning": "false_claim",
    "free_claim": "false_claim", "feature_claim": "missing_feature_claim",
}


async def run_external_factcheck(conn, source_id: int) -> dict:
    rules = [r for r in load_rules(conn)
             if r.get("current_value") and r.get("scope") in ("external", "both", None)]
    snips = conn.execute(
        """SELECT s.snippet, p.url, p.domain, p.id AS pid
           FROM external_snippets s JOIN external_pages p ON p.id=s.page_id
           WHERE s.source_id=? AND s.about_brand=1""",
        (source_id,),
    ).fetchall()

    conn.execute("DELETE FROM external_findings WHERE source_id=? AND kind='factcheck'", (source_id,))
    made = 0
    positive = unclear = 0  # for the pipeline FACT MATCH stage (✓ / ? counts)
    for rule in rules:
        terms = [t.lower() for t in (rule.get("stale_indicators", []) + rule.get("search_terms", []))]
        correct = rule["current_value"]
        cand = [{"snippet": s["snippet"], "url": s["url"], "domain": s["domain"], "pid": s["pid"]}
                for s in snips if not terms or any(t in (s["snippet"] or "").lower() for t in terms)]
        if not cand:
            continue
        await verdict(conn, cand, correct)  # sets c["verdict"]/["verdict_reason"]
        ft = _FT.get(rule["category"], "false_claim")
        for c in cand:
            v = c.get("verdict")
            if v == "matches":
                positive += 1
            elif v == "unclear":
                unclear += 1
            if v != "mismatch":
                continue
            conn.execute(
                """INSERT INTO external_findings
                     (source_id, kind, fact_name, external_url, domain, title, snippet, verdict,
                      reason, expected, finding_type, fact_rule, page_id, severity, status,
                      created_at, last_checked_at)
                   VALUES (?, 'factcheck', ?, ?, ?, ?, ?, 'mismatch', ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (source_id, rule.get("id"), c["url"], c["domain"], None, c["snippet"],
                 c.get("verdict_reason"), correct, ft, rule.get("id"), c["pid"],
                 rule.get("severity", "high"), now_iso(), now_iso()),
            )
            made += 1
    conn.commit()
    return {"rules": len(rules), "brand_snippets": len(snips), "findings": made,
            "positive": positive, "issue": made, "unclear": unclear}
