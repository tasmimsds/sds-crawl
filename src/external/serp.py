"""Bright Data SERP proxy client — Google search results as structured JSON.

Requests are routed through Bright Data's SERP proxy; appending brd_json=1
returns parsed results (organic list with title/link/description).
"""
from __future__ import annotations

from urllib.parse import quote_plus, urlparse

import httpx

from ..config import brightdata


def _proxy_url() -> str:
    bd = brightdata()
    return f"http://{bd['username']}:{bd['password']}@{bd['host']}:{bd['port']}"


def serp_search(query: str, num: int | None = None) -> list[dict]:
    """Return organic results [{title, url, snippet, domain}] for a query."""
    bd = brightdata()
    num = num or bd["results"]
    url = (
        f"https://www.google.com/search?q={quote_plus(query)}"
        f"&brd_json=1&num={num}&gl={bd['location']}&hl={bd['language']}"
    )
    with httpx.Client(proxy=_proxy_url(), verify=bd["verify_ssl"], timeout=45,
                      follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()

    out = []
    for o in data.get("organic", []):
        link = o.get("link") or o.get("url")
        if not link:
            continue
        out.append({
            "title": o.get("title") or "",
            "url": link,
            "snippet": o.get("description") or o.get("snippet") or "",
            "domain": (urlparse(link).hostname or "").replace("www.", ""),
        })
    return out
