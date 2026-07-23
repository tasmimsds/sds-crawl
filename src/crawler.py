"""Async crawler: status/redirects/timing + content & FAQ extraction into SQLite."""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urljoin

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import settings
from .db import add_faq, clear_faqs, fts_index_url, now_iso
from .extractor import extract
from .util import host_excluded, host_of

_REDIRECT_CODES = {301, 302, 303, 307, 308}


class _Retryable(Exception):
    def __init__(self, status: int):
        super().__init__(f"retryable status {status}")
        self.status = status


class _ResizableSemaphore:
    def __init__(self, capacity: int):
        self._capacity = capacity
        self._active = 0
        self._cond = asyncio.Condition()

    async def acquire(self):
        async with self._cond:
            while self._active >= self._capacity:
                await self._cond.wait()
            self._active += 1

    async def release(self):
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()

    async def set_capacity(self, n: int):
        async with self._cond:
            self._capacity = n
            self._cond.notify_all()


def _classify_error(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if "timeout" in name.lower() or "timeout" in msg:
        return "timeout"
    if "name or service" in msg or "nodename" in msg or "getaddrinfo" in msg:
        return "dns"
    if "certificate" in msg or "ssl" in msg or "tls" in msg:
        return "ssl"
    if "connect" in name.lower() or "connection" in msg:
        return "connection"
    return f"network: {name}"


async def _fetch(client, start_url, on_throttle, max_hops, retries, base_delay):
    chain: list[str] = []
    current = start_url
    t0 = time.monotonic()

    async def _hop(url):
        resp = await client.get(url)
        status = resp.status_code
        if status in (429, 503):
            await on_throttle()
            raise _Retryable(status)
        if status >= 500:
            raise _Retryable(status)
        return resp

    for _ in range(max_hops + 1):
        resp = None
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((_Retryable, httpx.TransportError)),
            stop=stop_after_attempt(retries + 1),
            wait=wait_exponential(multiplier=base_delay, min=base_delay, max=30),
            reraise=True,
        ):
            with attempt:
                resp = await _hop(current)
        assert resp is not None
        code = resp.status_code
        if code in _REDIRECT_CODES:
            loc = resp.headers.get("location")
            if not loc:
                break
            current = urljoin(current, loc)
            chain.append(current)
            continue
        ctype = resp.headers.get("content-type", "")
        html = resp.text if code < 400 and "html" in ctype else None
        return {
            "status_code": code,
            "final_url": current,
            "redirect_chain": chain,
            "response_time_ms": int((time.monotonic() - t0) * 1000),
            "error": None,
            "html": html,
        }
    return {
        "status_code": None,
        "final_url": current,
        "redirect_chain": chain,
        "response_time_ms": int((time.monotonic() - t0) * 1000),
        "error": f"too many redirects (>{max_hops})",
        "html": None,
    }


async def crawl_source(conn, source_id, *, only_changed=False, limit=None, concurrency=None,
                       on_progress=None, locales=None):
    from .db import source_domain
    c = settings()["crawl"]
    # per-project domain gate: crawl THIS project's own domain (not a global default)
    primary = source_domain(conn, source_id) or c["primary_domain"]
    conc = concurrency or c["concurrency"]

    rows = conn.execute(
        "SELECT id, url, lastmod, last_crawled, locale FROM urls WHERE source_id=? AND in_source=1 ORDER BY id",
        (source_id,),
    ).fetchall()
    # Only crawl the project's own domain; external alternates are recorded, not fetched.
    # Never crawl excluded hosts (e.g. admin55.sdsmanager.com).
    exclude = c.get("exclude_hosts") or []
    rows = [r for r in rows if (host_of(r["url"]) or "").endswith(primary)
            and not host_excluded(r["url"], exclude)]

    # Locale scope filter — applied BEFORE fetching, so excluded locales cost nothing
    # (no request, no extraction, no LLM). `locales` = allowed locale codes; '(root)'
    # covers no-locale/root pages (always kept unless explicitly excluded). None = all.
    if locales is not None:
        allow = set(locales)
        root_ok = "(root)" in allow
        before = len(rows)
        rows = [r for r in rows if (r["locale"] in allow) or (r["locale"] is None and root_ok)]
        print(f"Locale scope: crawling {len(rows)} of {before} URLs "
              f"(locales: {', '.join(sorted(allow)) or 'none'}).")

    skipped = 0
    if only_changed:
        keep = []
        for r in rows:
            if r["last_crawled"] and r["lastmod"] and r["lastmod"] <= r["last_crawled"]:
                skipped += 1
            else:
                keep.append(r)
        rows = keep
    if limit:
        rows = rows[:limit]

    print(f"Crawling {len(rows)} URLs (concurrency {conc}"
          + (f", {skipped} unchanged skipped" if only_changed else "") + ")...")

    sem = _ResizableSemaphore(conc)
    throttled = {"on": False}
    errors = {"n": 0}

    async def on_throttle():
        if not throttled["on"]:
            throttled["on"] = True
            await sem.set_capacity(c["throttle_concurrency"])
            print(f"  ! 429/503 — throttling to {c['throttle_concurrency']} concurrent, "
                  f"waiting {c['throttle_cooldown_s']}s")
        await asyncio.sleep(c["throttle_cooldown_s"])

    limits = httpx.Limits(max_connections=max(conc, 8))
    done = {"n": 0}
    total = len(rows)

    async with httpx.AsyncClient(
        http2=c["http2"],
        follow_redirects=False,
        timeout=c["request_timeout_s"],
        headers={"user-agent": c["user_agent"]},
        limits=limits,
    ) as client:

        async def worker(row):
            await sem.acquire()
            try:
                await asyncio.sleep(c["per_worker_delay_s"])
                try:
                    outcome = await _fetch(
                        client, row["url"], on_throttle,
                        c["max_redirect_hops"], c["max_retries"], c["retry_base_delay_s"],
                    )
                except Exception as exc:  # noqa: BLE001 - never crash the run
                    outcome = {
                        "status_code": None, "final_url": row["url"], "redirect_chain": [],
                        "response_time_ms": 0, "error": _classify_error(exc), "html": None,
                    }
                _store(conn, row["id"], outcome)
                if outcome["error"] or (outcome["status_code"] or 0) >= 400:
                    errors["n"] += 1
            finally:
                await sem.release()
                done["n"] += 1
                if on_progress:
                    on_progress(done["n"], total, errors["n"])
                if done["n"] % 25 == 0 or done["n"] == total:
                    print(f"  {done['n']}/{total} crawled")

        await asyncio.gather(*(worker(r) for r in rows))

    conn.commit()
    print(f"Crawl complete: {done['n']} URLs.")
    return done["n"]


def _store(conn, url_id, outcome):
    ex = extract(outcome["html"]) if outcome["html"] else None
    crawled_at = now_iso()
    conn.execute(
        """INSERT INTO crawl_results
             (url_id, crawled_at, status_code, final_url, redirect_chain, response_time_ms,
              error, content_hash, title, meta_description, canonical, meta_robots, h1, h2s,
              word_count, body_text)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            url_id, crawled_at, outcome["status_code"], outcome["final_url"],
            json.dumps(outcome["redirect_chain"]), outcome["response_time_ms"], outcome["error"],
            ex.content_hash if ex else None,
            ex.title if ex else None,
            ex.meta_description if ex else None,
            ex.canonical if ex else None,
            ex.meta_robots if ex else None,
            json.dumps(ex.h1s) if ex else None,
            json.dumps(ex.h2s) if ex else None,
            ex.word_count if ex else None,
            ex.body_text if ex else None,
        ),
    )
    conn.execute("UPDATE urls SET last_crawled=? WHERE id=?", (crawled_at, url_id))
    if ex:
        clear_faqs(conn, url_id)
        for faq in ex.faqs:
            add_faq(conn, url_id, faq.question, faq.answer, faq.source)
    # keep the FTS index in sync on every crawl write
    fts_index_url(conn, url_id)
