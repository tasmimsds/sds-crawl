"""SERP client — Google organic results as structured JSON for external fact-check.

Two interchangeable backends behind one `serp_search()` contract, each returning
[{title, url, snippet, domain}]:

  - DataForSEO  (https://api.dataforseo.com) — HTTP Basic, live Google organic SERP.
  - Bright Data (brd.superproxy.io)          — SERP proxy with brd_json=1.

`serp_provider()` picks the preferred backend (DataForSEO if configured, else Bright
Data; override with SERP_PROVIDER). If the preferred one raises, we fall back to the
other so a single provider outage doesn't kill external fact-check.
"""
from __future__ import annotations

import base64
from urllib.parse import quote_plus, urlparse

import httpx

from ..config import brightdata, dataforseo, dataforseo_enabled, serp_provider

# DataForSEO location codes: 2840 = United States, 2826 = United Kingdom, ...
# We map the 2-letter SERP_LOCATION to a code; default to US.
_DFS_LOCATION_CODES = {"us": 2840, "uk": 2826, "gb": 2826, "ca": 2124, "au": 2036,
                       "de": 2276, "fr": 2250, "nz": 2554, "eu": 2840}


def _domain(link: str) -> str:
    return (urlparse(link).hostname or "").replace("www.", "")


# ---- Bright Data backend --------------------------------------------------
def _serp_brightdata(query: str, num: int) -> list[dict]:
    bd = brightdata()
    if not (bd["username"] and bd["password"]):
        raise RuntimeError("Bright Data not configured")
    proxy = f"http://{bd['username']}:{bd['password']}@{bd['host']}:{bd['port']}"
    url = (f"https://www.google.com/search?q={quote_plus(query)}"
           f"&brd_json=1&num={num}&gl={bd['location']}&hl={bd['language']}")
    with httpx.Client(proxy=proxy, verify=bd["verify_ssl"], timeout=45,
                      follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    out = []
    for o in data.get("organic", []):
        link = o.get("link") or o.get("url")
        if not link:
            continue
        out.append({"title": o.get("title") or "", "url": link,
                    "snippet": o.get("description") or o.get("snippet") or "",
                    "domain": _domain(link)})
    return out


# ---- DataForSEO backend ---------------------------------------------------
def _serp_dataforseo(query: str, num: int) -> list[dict]:
    d = dataforseo()
    if not (d["login"] and d["password"]):
        raise RuntimeError("DataForSEO not configured")
    auth = base64.b64encode(f"{d['login']}:{d['password']}".encode()).decode()
    loc = _DFS_LOCATION_CODES.get((d["location"] or "us").lower(), 2840)
    payload = [{"keyword": query, "location_code": loc,
                "language_code": d["language"] or "en", "depth": num}]
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            "https://api.dataforseo.com/v3/serp/google/organic/live/regular",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
            json=payload)
        resp.raise_for_status()
        data = resp.json()
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO task error: {task.get('status_message')}")
    out = []
    for res in task.get("result") or []:
        for it in res.get("items") or []:
            if it.get("type") != "organic":
                continue
            link = it.get("url")
            if not link:
                continue
            out.append({"title": it.get("title") or "", "url": link,
                        "snippet": it.get("description") or it.get("snippet") or "",
                        "domain": it.get("domain") or _domain(link)})
    return out


_BACKENDS = {"dataforseo": _serp_dataforseo, "brightdata": _serp_brightdata}


def serp_search(query: str, num: int | None = None) -> list[dict]:
    """Organic results [{title, url, snippet, domain}]. Uses the preferred provider,
    falling back to the other if it errors (so one outage won't stop external checks)."""
    num = num or dataforseo()["results"]
    preferred = serp_provider()
    order = [preferred] + [b for b in ("dataforseo", "brightdata") if b != preferred]
    last_exc: Exception | None = None
    for name in order:
        try:
            return _BACKENDS[name](query, num)
        except Exception as exc:  # noqa: BLE001 — try the next provider
            last_exc = exc
            print(f"  ! SERP provider '{name}' failed: {exc}")
    raise RuntimeError(f"all SERP providers failed for query {query!r}: {last_exc}")
