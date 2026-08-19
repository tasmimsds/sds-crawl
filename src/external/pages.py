"""External page sources (manual URLs) + fetching them into external_pages."""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..db import now_iso
from ..extractor import extract
from .brand import ensure_brand_profile

# realistic browser headers reduce soft blocks (e.g. 415/406) on external sites
_HEADERS = {
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").replace("www.", "")


def discover_mentions(conn, source_id: int, queries: list[str] | None = None) -> dict:
    """Use the SERP proxy to find external pages mentioning the brand; store each
    organic result as a discovery page (text = the search snippet)."""
    from .serp import serp_search

    brand = ensure_brand_profile(conn, source_id)
    bn = brand["brand_name"]
    own = set(brand.get("own_domains") or [])
    queries = queries or [bn, f"{bn} review", f"{bn} pricing", f"{bn} vs", f"{bn} alternative"]
    added = 0
    for q in queries:
        try:
            results = serp_search(q)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! discovery query failed ({q}): {exc}")
            continue
        for r in results:
            dom = (r.get("domain") or "").replace("www.", "")
            if not dom or any(dom == o or dom.endswith("." + o) for o in own):
                continue
            # discovered pages are CANDIDATES until the user approves them
            cur = conn.execute(
                """INSERT INTO external_pages (source_id, url, domain, source_type, fetch_status,
                     fetched_at, title, text, created_at)
                   VALUES (?,?,?, 'discovery', 'candidate', ?, ?, ?, ?)
                   ON CONFLICT(source_id, url) DO UPDATE SET
                     title=excluded.title, text=excluded.text, fetched_at=excluded.fetched_at""",
                (source_id, r["url"], dom, now_iso(), r.get("title"), r.get("snippet"), now_iso()),
            )
            added += cur.rowcount
    conn.commit()
    return {"queries": queries, "added": added}


def add_external_urls(conn, source_id: int, urls: list[str], source_type: str = "manual") -> dict:
    """Register external page URLs (skips our own domains). Returns counts."""
    brand = ensure_brand_profile(conn, source_id)
    own = set(brand.get("own_domains") or [])
    added = skipped = 0
    for url in urls:
        url = url.strip()
        if not url.lower().startswith("http"):
            continue
        dom = _domain(url)
        if any(dom == o or dom.endswith("." + o) for o in own):
            skipped += 1  # that's an internal page, not external
            continue
        cur = conn.execute(
            """INSERT INTO external_pages (source_id, url, domain, source_type, fetch_status, created_at)
               VALUES (?,?,?,?, 'pending', ?)
               ON CONFLICT(source_id, url) DO NOTHING""",
            (source_id, url, dom, source_type, now_iso()),
        )
        added += cur.rowcount
    conn.commit()
    return {"added": added, "skipped_own_domain": skipped}


async def crawl_external_pages(conn, source_id: int, only_pending: bool = False,
                               on_progress=None) -> dict:
    """Fetch external pages, extract title/text; record blocked/error plainly. For
    backlink/mention pages, also extract the full PARAGRAPH around the anchor (linked)
    or the brand name (unlinked) so every external item has real surrounding context."""
    from ..extractor import extract_paragraph
    c = settings()["crawl"]
    brand = ensure_brand_profile(conn, source_id)
    brand_terms = [brand["brand_name"], *brand.get("aliases", [])]
    _MIN_CTX = 80
    q = ("SELECT id, url, source_type, mention_type, anchor_text, context_paragraph "
         "FROM external_pages WHERE source_id=?")
    if only_pending:
        q += " AND fetch_status='pending'"
    rows = conn.execute(q, (source_id,)).fetchall()
    ok = blocked = errored = 0
    total = len(rows)

    async with httpx.AsyncClient(follow_redirects=True, timeout=c["request_timeout_s"],
                                 headers=_HEADERS) as client:
        for i, r in enumerate(rows):
            status, err, title, text = "error", None, None, None
            para = r["context_paragraph"] or ""
            try:
                resp = await client.get(r["url"])
                if resp.status_code in (401, 403, 429, 503):
                    status, err = "blocked", "This site blocked automated access."
                elif resp.status_code >= 400:
                    status, err = "error", f"HTTP {resp.status_code}"
                elif "html" not in resp.headers.get("content-type", ""):
                    status, err = "error", "Not an HTML page."
                else:
                    ex = extract(resp.text)
                    status, title, text = "ok", ex.title, ex.body_text
                    # enrich context paragraph if thin (or missing)
                    if r["source_type"] in ("backlink", "mention") and len(para) < _MIN_CTX:
                        needle = r["anchor_text"] if r["mention_type"] == "linked" else None
                        candidates = [needle] if needle else []
                        candidates += brand_terms  # fall back to brand name / aliases
                        for n in candidates:
                            p = extract_paragraph(resp.text, n or "")
                            if p and len(p) >= _MIN_CTX:
                                para = p
                                break
                        else:
                            para = para or (candidates and extract_paragraph(resp.text, candidates[0])) or para
            except Exception as exc:  # noqa: BLE001
                status, err = "error", f"Could not reach the page ({type(exc).__name__})."
            conn.execute(
                """UPDATE external_pages SET fetch_status=?, fetch_error=?, fetched_at=?,
                     title=?, text=?, context_paragraph=? WHERE id=?""",
                (status, err, now_iso(), title, text, para or None, r["id"]),
            )
            conn.commit()
            ok += status == "ok"
            blocked += status == "blocked"
            errored += status == "error"
            if on_progress:
                on_progress(i + 1, total, blocked + errored)
            await asyncio.sleep(c["per_worker_delay_s"])  # polite
    return {"total": total, "ok": ok, "blocked": blocked, "errored": errored}
