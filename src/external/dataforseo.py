"""DataForSEO client for EXTERNAL discovery — backlinks + brand mentions.

One place that talks to DataForSEO. Everything returns plain dicts and reports the
API cost so the caller can show/track spend. Missing keys -> enabled() is False and
callers fall back to a "connect DataForSEO" state (manual URLs still work).
"""
from __future__ import annotations

import base64
import json

import httpx

from ..config import dataforseo

_BASE = "https://api.dataforseo.com/v3"


def enabled() -> bool:
    d = dataforseo()
    return bool(d["login"] and d["password"])


def _auth() -> str:
    d = dataforseo()
    return base64.b64encode(f"{d['login']}:{d['password']}".encode()).decode()


def _post(path: str, payload: list) -> dict:
    with httpx.Client(timeout=90) as client:
        r = client.post(_BASE + path, headers={"Authorization": f"Basic {_auth()}",
                                                "Content-Type": "application/json"}, json=payload)
        r.raise_for_status()
        return r.json()


def _task(data: dict) -> tuple[dict, float]:
    """First task's result[0] + this call's cost (0 on error)."""
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO: {task.get('status_message')}")
    res = (task.get("result") or [{}])[0] or {}
    return res, float(task.get("cost") or 0)


_PAGE = 1000        # DataForSEO backlinks/live max page size
_HARD_MAX = 30000   # safety ceiling on the list pull (millions of self-links guard)


def backlinks_summary(domain: str) -> dict:
    """Cheap TRUE totals (no per-row billing): total backlinks + referring domains."""
    res, cost = _task(_post("/backlinks/summary/live",
                            [{"target": domain, "backlinks_status_type": "live",
                              "include_subdomains": True}]))
    return {"backlinks": res.get("backlinks") or 0,
            "referring_domains": res.get("referring_domains") or 0, "cost": cost}


def fetch_backlinks(domain: str, max_rows: int | None = None,
                    exclude_domains: set[str] | None = None, mode: str = "one_per_domain") -> dict:
    """ALL referring pages that link to `domain`, FULLY paginated (offset/limit loop
    until total_count) — never stops at page 1. Deduped by referring URL, own domains
    excluded. Keeps ONLY {url, anchor, context, dofollow, first_seen, title}.

    mode: 'one_per_domain' (default) = the complete set of distinct referring DOMAINS
    (every external site that links us — the useful external footprint, cheap); 'as_is'
    = every page-level backlink (includes many self-links/dupes). max_rows caps the pull."""
    exclude = {d.replace("www.", "") for d in (exclude_domains or set())}
    ceiling = min(max_rows or _HARD_MAX, _HARD_MAX)
    out, seen = [], set()
    total_count, cost, offset = 0, 0.0, 0
    while offset < ceiling:
        res, c = _task(_post("/backlinks/backlinks/live",
                             [{"target": domain, "mode": mode, "limit": _PAGE,
                               "offset": offset, "backlinks_status_type": "live"}]))
        cost += c
        total_count = res.get("total_count") or total_count
        items = res.get("items") or []
        if not items:
            break
        for it in items:
            dom = (it.get("domain_from") or "").replace("www.", "")
            url = it.get("url_from") or ""
            if not url or not dom or url in seen:
                continue
            if any(dom == e or dom.endswith("." + e) for e in exclude):
                continue  # our own domain / regional variant — internal, not external
            seen.add(url)
            anchor = (it.get("anchor") or "").strip()
            pre = (it.get("text_pre") or "").strip()
            post = (it.get("text_post") or "").strip()
            context = " ".join(p for p in (pre, anchor, post) if p).strip()
            out.append({"url": url, "domain": dom, "anchor": anchor, "context": context,
                        "dofollow": bool(it.get("dofollow")), "first_seen": it.get("first_seen"),
                        "title": it.get("page_from_title") or ""})
        offset += _PAGE
        if offset >= (total_count or 0):
            break
    return {"items": out, "total": total_count, "retrieved": len(out), "cost": round(cost, 4)}


def fetch_serp_mentions(query: str, limit: int, exclude_domains: set[str] | None = None) -> dict:
    """Organic results for a brand query (live Google), for mention discovery.
    Returns {items:[{url, domain, snippet, title}], cost}."""
    from ..config import dataforseo as _cfg
    d = _cfg()
    loc = {"us": 2840, "uk": 2826, "gb": 2826, "ca": 2124, "au": 2036,
           "de": 2276, "nz": 2554}.get((d["location"] or "us").lower(), 2840)
    res, cost = _task(_post("/serp/google/organic/live/regular",
                            [{"keyword": query, "location_code": loc,
                              "language_code": d["language"] or "en", "depth": limit}]))
    exclude = {x.replace("www.", "") for x in (exclude_domains or set())}
    out = []
    for it in res.get("items") or []:
        if it.get("type") != "organic":
            continue
        url = it.get("url")
        dom = (it.get("domain") or "").replace("www.", "")
        if not url or not dom or any(dom == e or dom.endswith("." + e) for e in exclude):
            continue
        out.append({"url": url, "domain": dom,
                    "snippet": it.get("description") or it.get("snippet") or "",
                    "title": it.get("title") or ""})
    return {"items": out, "cost": cost}


def ai_mentions_available() -> bool:
    """Whether this account exposes AI/LLM mention data. We probe cheaply and skip
    gracefully if not enabled (never fabricate AI-mention data)."""
    try:
        # DataForSEO 'ai_optimization' is a paid add-on; a cheap errors-if-absent probe.
        data = _post("/appendix/user_data", [])
        res, _ = _task(data)
        rates = json.dumps(res.get("price") or {})
        return "ai_optimization" in rates or "llm_responses" in rates
    except Exception:  # noqa: BLE001
        return False
