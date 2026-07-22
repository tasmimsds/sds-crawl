"""Technical layer: status / crawl / seo_technical issues from crawl data."""
from __future__ import annotations

import json
import re

from ..config import settings
from ..db import latest_crawl_join, record_issue


def _norm_url(u: str) -> str:
    return re.sub(r"/+$", "", re.sub(r"^https?://www\.", "https://", u or ""))


def analyze_technical(conn, source_id: int) -> int:
    th = settings()["thresholds"]
    rows = latest_crawl_join(conn, source_id)
    issues = 0

    # duplicate titles within locale
    title_groups: dict[str, list[int]] = {}
    for r in rows:
        if r["title"] and r["status_code"] and r["status_code"] < 400:
            key = f"{r['locale'] or ''}|{r['title'].lower()}"
            title_groups.setdefault(key, []).append(r["url_id"])

    def issue(**kw):
        nonlocal issues
        record_issue(conn, source_id=source_id, detection_method="crawl", **kw)
        issues += 1

    for r in rows:
        uid = r["url_id"]

        if r["error"]:
            issue(url_id=uid, category="crawl", severity="critical",
                  title=f"crawl_error:{r['error'].split(':')[0]}",
                  detail=f"Crawl failed: {r['error']}")
            continue

        code = r["status_code"]
        if code is not None and code >= 400:
            issue(url_id=uid, category="status", severity="critical",
                  title=f"http_{code}", detail=f"URL returned HTTP {code}.",
                  evidence=r["final_url"])
            continue

        chain = json.loads(r["redirect_chain"]) if r["redirect_chain"] else []
        if len(chain) > 1:
            issue(url_id=uid, category="seo_technical", severity="high",
                  title="redirect_chain",
                  detail=f"URL redirects through {len(chain)} hops before resolving.",
                  evidence=" → ".join([r["url"], *chain]),
                  expected="Source URLs should return 200 directly.")
        elif len(chain) == 1:
            issue(url_id=uid, category="seo_technical", severity="medium",
                  title="redirect", detail="URL redirects instead of returning 200 directly.",
                  evidence=f"{r['url']} → {chain[0]}",
                  expected="Source URLs should return 200 directly.")

        if r["meta_robots"] and re.search(r"noindex", r["meta_robots"], re.I):
            issue(url_id=uid, category="seo_technical", severity="high",
                  title="noindex_in_source", detail="Indexable source URL is marked noindex.",
                  evidence=r["meta_robots"])

        if r["canonical"] and _norm_url(r["canonical"]) != _norm_url(r["url"]):
            issue(url_id=uid, category="seo_technical", severity="high",
                  title="canonical_mismatch",
                  detail="Canonical points to a different URL than this source URL.",
                  evidence=r["canonical"], expected=r["url"])

        if not r["title"]:
            issue(url_id=uid, category="seo_technical", severity="medium",
                  title="missing_title", detail="Missing or empty <title>.")
        if not r["meta_description"]:
            issue(url_id=uid, category="seo_technical", severity="medium",
                  title="missing_meta_description", detail="Missing or empty meta description.")

        h1s = json.loads(r["h1"]) if r["h1"] else []
        if len(h1s) == 0:
            issue(url_id=uid, category="seo_technical", severity="medium",
                  title="missing_h1", detail="Page has no H1.")
        elif len(h1s) > 1:
            issue(url_id=uid, category="seo_technical", severity="medium",
                  title="multiple_h1", detail=f"Page has {len(h1s)} H1 elements.",
                  evidence=" | ".join(h1s))

        if r["title"]:
            key = f"{r['locale'] or ''}|{r['title'].lower()}"
            grp = title_groups.get(key, [])
            if len(grp) > 1:
                issue(url_id=uid, category="seo_technical", severity="medium",
                      title="duplicate_title_in_locale",
                      detail=f"Title shared by {len(grp)} URLs in locale '{r['locale'] or ''}'.",
                      evidence=r["title"])

        wc = r["word_count"]
        if wc is not None and wc < th["thin_content_words"]:
            issue(url_id=uid, category="seo_technical", severity="low",
                  title="thin_content",
                  detail=f"Only {wc} words of main content (threshold {th['thin_content_words']}).")

        rt = r["response_time_ms"]
        if rt is not None and rt > th["slow_response_ms"]:
            issue(url_id=uid, category="seo_technical", severity="low",
                  title="slow_response",
                  detail=f"Response took {rt}ms (threshold {th['slow_response_ms']}ms).")

    conn.commit()
    print(f"Technical analysis: {issues} issues across {len(rows)} pages.")
    return issues
