"""Run a structured fact against the active site: FTS -> cheap classify ->
LLM verdict on ambiguous only -> results + write mismatches to issues."""
from __future__ import annotations

from ..db import record_issue
from .query import _MARK, save_query, search, verdict


def _cheap_classify(row, correct, stale, allowed) -> tuple[str | None, str]:
    """Return (verdict, reason) if decidable cheaply, else (None, '')."""
    text = _MARK.sub("", row["snippet"]).lower()
    has_allowed = any(a and a in text for a in allowed)
    has_stale = any(s and s in text for s in stale)
    if has_stale and not has_allowed:
        return "mismatch", "Contains an outdated/incorrect value."
    if has_allowed and not has_stale and not (correct and correct not in text):
        return "matches", "Only an allowed mention is present."
    if correct and correct in text and not has_stale:
        return "matches", "States the correct value."
    return None, ""


async def run_fact(conn, source_id: int, fact: dict) -> dict:
    terms = fact.get("search_terms") or [fact.get("claim_topic") or fact.get("fact_name")]
    correct = (fact.get("correct_value") or "").strip()
    stale = [s.lower() for s in fact.get("stale_indicators", [])]
    allowed = [a.lower() for a in fact.get("allowed_mentions", [])]
    severity = fact.get("severity") or "high"
    category = fact.get("category") or "other_mismatch"

    result = search(conn, source_id, terms, limit=300)
    rows = result["rows"]

    # hreflang group per matched url
    ids = [r["url_id"] for r in rows if r.get("url_id")]
    hg = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        hg = {r["id"]: r["hgid"] for r in conn.execute(
            f"SELECT id, hreflang_group_id AS hgid FROM urls WHERE id IN ({marks})", ids)}

    ambiguous = []
    for r in rows:
        r["hg"] = hg.get(r["url_id"])
        v, reason = _cheap_classify(r, correct.lower(), stale, allowed)
        if v:
            r["verdict"], r["verdict_reason"], r["how"] = v, reason, "rule"
        elif correct:
            ambiguous.append(r)
        else:
            r["verdict"], r["verdict_reason"], r["how"] = "unclear", "No correct value set.", "rule"

    if correct and ambiguous:
        await verdict(conn, ambiguous, correct)  # sets r["verdict"]/["verdict_reason"]
        for r in ambiguous:
            r["how"] = "llm"

    counts = {"mismatch": 0, "matches": 0, "unclear": 0, "unrelated": 0}
    for r in rows:
        counts[r.get("verdict") or "unclear"] = counts.get(r.get("verdict") or "unclear", 0) + 1

    # write confirmed mismatches to issues
    query_id = save_query(conn, fact.get("fact_name") or (terms[0] if terms else "fact"),
                          terms, correct)
    issues_made = 0
    for r in rows:
        if r.get("verdict") != "mismatch" or not r.get("url_id"):
            continue
        record_issue(
            conn, source_id=source_id, url_id=r["url_id"], category=category, severity=severity,
            title=f"fact:{(fact.get('fact_name') or 'fact')[:48]}",
            detail=r.get("verdict_reason") or f"Contradicts expected value: {correct}",
            evidence=_MARK.sub("", r["snippet"]), expected=correct or None,
            detection_method=r.get("how", "llm"),
        )
        title = f"fact:{(fact.get('fact_name') or 'fact')[:48]}"
        conn.execute("UPDATE issues SET query_id=? WHERE source_id=? AND url_id=? AND title=?",
                     (query_id, source_id, r["url_id"], title))
        row = conn.execute("SELECT id FROM issues WHERE source_id=? AND url_id=? AND title=? AND related_url_id=0",
                           (source_id, r["url_id"], title)).fetchone()
        r["issue_id"] = row["id"] if row else None
        issues_made += 1
    conn.commit()

    pages_crawled = conn.execute(
        "SELECT COUNT(DISTINCT c.url_id) c FROM crawl_results c JOIN urls u ON u.id=c.url_id WHERE u.source_id=?",
        (source_id,)).fetchone()["c"]

    return {"fact": fact, "terms": terms, "pages_crawled": pages_crawled,
            "mention": len(rows), "counts": counts, "rows": rows,
            "issues_made": issues_made, "query_id": query_id}
