"""Deterministic regex fact engine (Issues 1-3). Free, runs on ALL locales."""
from __future__ import annotations

import re

from ..config import settings
from ..db import record_issue
from ..rules import load_rules
from ..util import context_around


def _latest_bodies(conn, source_id):
    return conn.execute(
        """SELECT u.id AS url_id, u.url, u.locale, c.body_text
           FROM urls u
           JOIN crawl_results c ON c.id = (
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1
           )
           WHERE u.source_id=? AND c.body_text IS NOT NULL AND c.body_text != ''""",
        (source_id,),
    ).fetchall()


def _is_english(locale, english_locales) -> bool:
    # No-locale top-level pages are the primary English site.
    return locale is None or locale in english_locales


def _passes_context(body, m, require_ctx, allowed, window) -> bool:
    if not require_ctx and not allowed:
        return True
    seg = body[max(0, m.start() - window): m.end() + window]
    if require_ctx and not any(rx.search(seg) for rx in require_ctx):
        return False
    if allowed and any(rx.search(seg) for rx in allowed):
        return False
    return True


def analyze_facts_regex(conn, source_id: int) -> int:
    english = set(settings()["llm"]["english_locales"])
    rows = _latest_bodies(conn, source_id)
    issues = 0

    compiled = []
    for f in load_rules(conn):
        if f.get("type") != "stale":
            continue
        compiled.append({
            "fact": f,
            "stale": [re.compile(p, re.I) for p in f.get("stale_patterns", [])],
            "require": [re.compile(p, re.I) for p in f.get("require_context", [])],
            "allowed": [re.compile(p, re.I) for p in f.get("allowed_patterns", [])],
            "window": f.get("context_window", 120),
        })

    for page in rows:
        body = page["body_text"]
        is_en = _is_english(page["locale"], english)
        for c in compiled:
            f = c["fact"]
            if f.get("applies_to") == "english_only" and not is_en:
                continue
            hit = None
            for rx in c["stale"]:
                for m in rx.finditer(body):
                    if _passes_context(body, m, c["require"], c["allowed"], c["window"]):
                        hit = m
                        break
                if hit:
                    break
            if hit:
                record_issue(
                    conn,
                    source_id=source_id,
                    url_id=page["url_id"],
                    category=f["category"],
                    severity=f["severity"],
                    title=f"{f['id']}:stale",
                    detail=f["description"],
                    evidence=context_around(body, hit.start(), hit.end() - hit.start()),
                    expected=f.get("current_value"),
                    detection_method="regex",
                )
                issues += 1
    conn.commit()
    print(f"Facts (regex) analysis: {issues} issues across {len(rows)} pages.")
    return issues
