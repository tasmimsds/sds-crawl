"""Bright Data SERP client — search-engine brand/mention discovery (Google + Bing).

Routes Google/Bing searches through Bright Data's SERP proxy (brd_json=1 -> parsed
JSON), paginating past page 1 so we find ALL mentions, not a page-1 sample. One place
that talks to Bright Data; graceful "connect Bright Data" state when creds are missing.
"""
from __future__ import annotations

import json
from urllib.parse import quote_plus, urlparse

import httpx

from ..config import brightdata_serp

_ENGINES = {
    # engine -> (base url, page-offset param, page size)
    "google": ("https://www.google.com/search", "start"),
    "bing": ("https://www.bing.com/search", "first"),
}


def enabled() -> bool:
    c = brightdata_serp()
    return bool(c["username"] and c["password"])


def _proxy(c) -> str:
    return f"http://{c['username']}:{c['password']}@{c['host']}:{c['port']}"


def _domain(link: str) -> str:
    return (urlparse(link).hostname or "").replace("www.", "")


def serp_page(query: str, engine: str, page: int, per_page: int, client) -> list[dict]:
    """One SERP page of organic results [{url, domain, title, snippet}]."""
    base, param = _ENGINES.get(engine, _ENGINES["google"])
    # google: start=0,20,40…  ·  bing: first=1,21,41…
    offset = page * per_page + (1 if engine == "bing" else 0)
    url = f"{base}?q={quote_plus(query)}&{param}={offset}&brd_json=1"
    if engine == "google":
        url += f"&num={per_page}"
    resp = client.get(url)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for o in data.get("organic") or []:
        link = o.get("link") or o.get("url")
        if not link:
            continue
        out.append({"url": link, "domain": _domain(link),
                    "title": o.get("title") or "",
                    "snippet": o.get("description") or o.get("snippet") or ""})
    return out


def discover_mentions(queries: list[str], *, engines=None, pages=None, per_page=None,
                      exclude_domains: set[str] | None = None) -> dict:
    """Search Google + Bing for every query, paginate, dedupe URLs across queries/engines,
    exclude own domains. Returns {items:[{url,domain,title,snippet,engine}], per_engine}."""
    c = brightdata_serp()
    engines = engines or c["engines"]
    pages = pages or c["pages"]
    per_page = per_page or c["per_page"]
    exclude = {d.replace("www.", "") for d in (exclude_domains or set())}
    items, seen, per_engine = [], set(), {}
    with httpx.Client(proxy=_proxy(c), verify=c["verify_ssl"], timeout=45,
                      follow_redirects=True) as client:
        for engine in engines:
            if engine not in _ENGINES:
                continue
            per_engine[engine] = 0
            for q in queries:
                for page in range(pages):
                    try:
                        rows = serp_page(q, engine, page, per_page, client)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  ! {engine} SERP failed ({q} p{page}): {exc}")
                        break  # stop paginating this query on error
                    if not rows:
                        break  # no more results for this query
                    for r in rows:
                        dom = r["domain"]
                        if not dom or r["url"] in seen:
                            continue
                        if any(dom == e or dom.endswith("." + e) for e in exclude):
                            continue
                        seen.add(r["url"])
                        r["engine"] = engine
                        items.append(r)
                        per_engine[engine] += 1
    return {"items": items, "per_engine": per_engine, "total": len(items)}
