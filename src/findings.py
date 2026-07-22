"""Findings management for internal issues: recheck, edit, false-positive, delete."""
from __future__ import annotations

import json
import re

import httpx

from .config import settings
from .db import now_iso
from .extractor import extract
from .util import normalize_text

_HEADERS = {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8", "accept-language": "en-US,en;q=0.9"}


def _evidence_core(evidence: str) -> str:
    """The real page text inside an evidence quote (strip … and <mark>)."""
    e = re.sub(r"</?mark>", "", evidence or "")
    e = e.strip("… ").strip()
    return normalize_text(e)[:80]


def recheck_issue(conn, issue_id: int) -> dict:
    """Refetch the page live and re-check whether the evidence is still present."""
    row = conn.execute(
        "SELECT i.*, u.url FROM issues i JOIN urls u ON u.id=i.url_id WHERE i.id=?", (issue_id,)
    ).fetchone()
    if not row:
        return {"error": "not found"}
    ts = now_iso()
    try:
        r = httpx.get(row["url"], headers=_HEADERS, follow_redirects=True,
                      timeout=settings()["crawl"]["request_timeout_s"])
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            raise RuntimeError(f"HTTP {r.status_code}")
        body = normalize_text(extract(r.text).body_text)
    except Exception as exc:  # noqa: BLE001
        conn.execute("UPDATE issues SET status='unverifiable', last_checked_at=?, "
                     "note=? WHERE id=?", (ts, f"Page unreachable: {exc}", issue_id))
        conn.commit()
        return {"status": "unverifiable", "message": f"Page unreachable ({exc})."}

    core = _evidence_core(row["evidence"] or "")
    still = bool(core) and core.lower() in body.lower()
    if still:
        conn.execute("UPDATE issues SET status='open', last_checked_at=?, note=? WHERE id=?",
                     (ts, f"Still present as of {ts[:16].replace('T', ' ')}", issue_id))
        outcome = {"status": "open", "message": "Still present — issue remains open."}
    else:
        conn.execute("UPDATE issues SET status='fixed', last_checked_at=?, note=? WHERE id=?",
                     (ts, f"No longer present as of {ts[:16].replace('T', ' ')}", issue_id))
        outcome = {"status": "fixed", "message": "No longer present — marked fixed."}
    conn.commit()
    return outcome


def edit_issue(conn, issue_id: int, severity=None, category=None, expected=None, note=None) -> None:
    row = conn.execute("SELECT severity, category, expected, original_snapshot FROM issues WHERE id=?",
                       (issue_id,)).fetchone()
    if not row:
        return
    snap = row["original_snapshot"] or json.dumps(
        {"severity": row["severity"], "category": row["category"], "expected": row["expected"]})
    conn.execute(
        """UPDATE issues SET severity=COALESCE(?,severity), category=COALESCE(?,category),
             expected=COALESCE(?,expected), note=COALESCE(?,note), edited=1, original_snapshot=?
           WHERE id=?""",
        (severity, category, expected, note, snap, issue_id))
    conn.commit()


def false_positive(conn, issue_id: int, reason: str = "", dont_flag: bool = False,
                   phrase: str = "") -> dict:
    row = conn.execute("SELECT title, evidence FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        return {"error": "not found"}
    conn.execute("UPDATE issues SET status='false_positive', note=?, last_checked_at=? WHERE id=?",
                 (reason or "Marked not an issue", now_iso(), issue_id))
    added_to = None
    if dont_flag:
        slug = re.sub(r"^(stale_fact:|llm:|rule:|fact:|query:)", "", row["title"] or "").split(":")[0]
        rule = conn.execute("SELECT id, slug, allowed_patterns FROM fact_rules WHERE slug=?",
                            (slug,)).fetchone()
        key = (phrase or _evidence_core(row["evidence"] or "")).strip()
        if rule and key:
            allowed = json.loads(rule["allowed_patterns"] or "[]")
            if key not in allowed:
                allowed.append(key)
                conn.execute("UPDATE fact_rules SET allowed_patterns=? WHERE id=?",
                             (json.dumps(allowed), rule["id"]))
                added_to = rule["slug"]
    conn.commit()
    return {"status": "false_positive", "allowed_added_to": added_to}


def delete_issue(conn, issue_id: int) -> None:
    conn.execute("UPDATE issues SET deleted_at=? WHERE id=?", (now_iso(), issue_id))
    conn.commit()


def recheck_external_finding(conn, finding_id: int) -> dict:
    """Refetch the external page live and re-check whether the claim is still present."""
    row = conn.execute("SELECT external_url, snippet FROM external_findings WHERE id=?",
                       (finding_id,)).fetchone()
    if not row:
        return {"error": "not found"}
    ts = now_iso()
    try:
        r = httpx.get(row["external_url"], headers=_HEADERS, follow_redirects=True,
                      timeout=settings()["crawl"]["request_timeout_s"])
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            raise RuntimeError(f"HTTP {r.status_code}")
        body = normalize_text(extract(r.text).body_text)
    except Exception as exc:  # noqa: BLE001
        conn.execute("UPDATE external_findings SET status='unverifiable', last_checked_at=?, note=? WHERE id=?",
                     (ts, f"Page unreachable: {exc}", finding_id))
        conn.commit()
        return {"status": "unverifiable", "message": f"Page unreachable ({exc})."}
    core = _evidence_core(row["snippet"] or "")
    still = bool(core) and core.lower() in body.lower()
    status = "open" if still else "fixed"
    conn.execute("UPDATE external_findings SET status=?, last_checked_at=?, note=? WHERE id=?",
                 (status, ts, "Still present" if still else "No longer present", finding_id))
    conn.commit()
    return {"status": status, "message": "Still present." if still else "No longer present — fixed."}
