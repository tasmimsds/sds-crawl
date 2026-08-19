"""External MATCH & SORT — for every brand-relevant passage, classify into:
  ✗ fact mismatch  -> external_findings (Issues, actionable)
  ✓ fact correct   -> positive count (our message is accurate out there)
  📋 other brand info -> general_facts (review bucket the user triages; NOT auto-issues)
"""
from __future__ import annotations

from urllib.parse import urlparse

from ..db import now_iso
from ..factcheck.query import verdict
from ..rules import load_rules

# fact category -> external finding_type
_FT = {
    "database_size": "outdated_info", "language_count": "outdated_info",
    "region_count": "outdated_info", "regulation_count": "outdated_info",
    "positioning": "false_claim", "free_claim": "false_claim",
    "feature_claim": "missing_feature_claim",
}
# domains that are reviews / listings -> source_kind for General Facts
_REVIEW_SITES = {"trustpilot.com", "g2.com", "capterra.com", "getapp.com", "trustradius.com",
                 "softwareadvice.com", "producthunt.com", "reddit.com", "sourceforge.net",
                 "glassdoor.com"}
_LISTING_SITES = {"crunchbase.com", "goodfirms.co", "saashub.com", "slashdot.org",
                  "alternativeto.net", "clutch.co"}


def _source_kind(domain: str, page_source_type: str | None) -> str:
    d = (domain or "").replace("www.", "")
    if any(d == s or d.endswith("." + s) for s in _REVIEW_SITES):
        return "review"
    if any(d == s or d.endswith("." + s) for s in _LISTING_SITES):
        return "listing"
    return page_source_type or "mention"  # backlink | mention


async def run_external_factcheck(conn, source_id: int) -> dict:
    rules = [r for r in load_rules(conn)
             if r.get("current_value") and r.get("scope") in ("external", "both", None)]
    snips = conn.execute(
        """SELECT s.id AS sid, s.snippet, p.url, p.domain, p.id AS pid, p.source_type
           FROM external_snippets s JOIN external_pages p ON p.id=s.page_id
           WHERE s.source_id=? AND s.about_brand=1""",
        (source_id,),
    ).fetchall()

    conn.execute("DELETE FROM external_findings WHERE source_id=? AND kind='factcheck'", (source_id,))
    made = positive = unclear = 0
    claimed: set[int] = set()  # snippet ids that mapped to a defined fact (either verdict)

    for rule in rules:
        terms = [t.lower() for t in (rule.get("stale_indicators", []) + rule.get("search_terms", []))]
        correct = rule["current_value"]
        cand = [{"sid": s["sid"], "snippet": s["snippet"], "url": s["url"], "domain": s["domain"],
                 "pid": s["pid"]}
                for s in snips if not terms or any(t in (s["snippet"] or "").lower() for t in terms)]
        if not cand:
            continue
        await verdict(conn, cand, correct)  # sets c["verdict"] / c["verdict_reason"]
        ft = _FT.get(rule["category"], "false_claim")
        for c in cand:
            v = c.get("verdict")
            if v in ("mismatch", "matches"):
                claimed.add(c["sid"])
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

    # 📋 GENERAL FACTS — brand passages that touched NO defined fact. Preserve any the
    # user already triaged (flag/note/promoted/dismissed); only refresh untouched ones.
    conn.execute(
        """DELETE FROM general_facts WHERE source_id=? AND status='open'
             AND needs_change='undecided' AND (note IS NULL OR note='')
             AND promoted_fact_id IS NULL AND issue_id IS NULL""", (source_id,))
    general = 0
    for s in snips:
        if s["sid"] in claimed:
            continue
        kind = _source_kind(s["domain"], s["source_type"])
        cur = conn.execute(
            """INSERT INTO general_facts (source_id, page_id, quote, source_url, domain,
                 source_kind, needs_change, status, created_at)
               VALUES (?,?,?,?,?,?, 'undecided', 'open', ?)
               ON CONFLICT(source_id, source_url, quote) DO NOTHING""",
            (source_id, s["pid"], s["snippet"], s["url"], s["domain"], kind, now_iso()))
        general += cur.rowcount
    conn.commit()
    return {"rules": len(rules), "brand_snippets": len(snips), "findings": made,
            "positive": positive, "issue": made, "unclear": unclear, "general": general}
