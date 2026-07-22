"""Background jobs (crawl + full sync) with progress persisted in `jobs`."""
from __future__ import annotations

import asyncio

from starlette.concurrency import run_in_threadpool

from .crawler import crawl_source
from .db import CATEGORIES, now_iso, reconcile_fixed


def create_job(conn, type_: str, source_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (type, source_id, status, created_at) VALUES (?,?,'queued',?)",
        (type_, source_id, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def update_job(conn, job_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


def get_job(conn, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def latest_job(conn, source_id: int, type_: str | None = None):
    q = "SELECT * FROM jobs WHERE source_id=?"
    args = [source_id]
    if type_:
        q += " AND type=?"
        args.append(type_)
    q += " ORDER BY id DESC LIMIT 1"
    return conn.execute(q, args).fetchone()


async def run_crawl_job(conn, job_id: int, source_id: int, *, only_changed=False,
                        limit=None, concurrency=None) -> None:
    """Run a crawl as a background task, streaming progress into the jobs row."""
    update_job(conn, job_id, status="running", started_at=now_iso(),
               message="Starting crawl…")

    def on_progress(done: int, total: int, errors: int) -> None:
        update_job(conn, job_id, progress=done, total=total, errors=errors,
                   message=f"Crawled {done} of {total} pages" + (f" ({errors} errors)" if errors else ""))

    try:
        await crawl_source(conn, source_id, only_changed=only_changed, limit=limit,
                           concurrency=concurrency, on_progress=on_progress)
        update_job(conn, job_id, status="done", finished_at=now_iso(),
                   message="Crawl complete.")
    except Exception as exc:  # noqa: BLE001
        update_job(conn, job_id, status="error", finished_at=now_iso(),
                   error=str(exc), message="Crawl failed.")


# ---- full sync pipeline: sitemap -> pages -> facts + technical, one click ----

def _in_source_ids(conn, source_id):
    return {r["id"] for r in conn.execute(
        "SELECT id FROM urls WHERE source_id=? AND in_source=1", (source_id,))}


def _latest_hashes(conn, source_id):
    return {r["url_id"]: r["h"] for r in conn.execute(
        """SELECT u.id AS url_id, (SELECT content_hash FROM crawl_results
             WHERE url_id=u.id ORDER BY id DESC LIMIT 1) AS h
           FROM urls u WHERE u.source_id=?""", (source_id,)) if r["h"]}


async def run_sync_job(conn, job_id: int, source_id: int, *, only_changed=False,
                       include_external=False) -> None:
    """One click = check sitemap, crawl pages, then run fact + technical checks.

    include_external (scheduled syncs) also refreshes external/web findings.
    """
    from .analysis.facts import analyze_facts_regex
    from .analysis.inventory import consistency_check
    from .analysis.technical import analyze_technical
    from .ingest import add_and_ingest

    run_start = now_iso()
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    update_job(conn, job_id, status="running", started_at=run_start, stage="sitemap",
               progress=0, total=0, message="Checking site map…")
    before_in = _in_source_ids(conn, source_id)
    before_hashes = _latest_hashes(conn, source_id)

    try:
        # Stage 1 — refresh the sitemap to catch new / removed URLs
        if src["kind"] in ("sitemap", "root"):
            await run_in_threadpool(add_and_ingest, conn, src["location"], src["name"])
        after_in = _in_source_ids(conn, source_id)
        update_job(conn, job_id, urls_new=len(after_in - before_in),
                   urls_removed=len(before_in - after_in))

        # Stage 2 — crawl the pages
        update_job(conn, job_id, stage="pages", message="Checking pages…", progress=0)

        def on_progress(done, total, errors):
            update_job(conn, job_id, progress=done, total=total, errors=errors,
                       message=f"Checking pages… {done}/{total}")

        await crawl_source(conn, source_id, only_changed=only_changed, on_progress=on_progress)

        after_hashes = _latest_hashes(conn, source_id)
        changed = sum(1 for uid, h in after_hashes.items()
                      if uid in before_hashes and before_hashes[uid] != h)
        update_job(conn, job_id, urls_changed=changed)

        # Stage 3 — fact rules + inventory + technical (deterministic) + query rules (LLM)
        update_job(conn, job_id, stage="facts", message="Checking facts…")
        await run_in_threadpool(analyze_technical, conn, source_id)
        await run_in_threadpool(analyze_facts_regex, conn, source_id)
        await run_in_threadpool(consistency_check, conn, source_id)
        from .factcheck.scan import run_query_rules
        await run_query_rules(conn, source_id)

        # Stage 4 (scheduled runs) — external / web findings
        if include_external:
            from .config import serp_enabled
            if serp_enabled():
                update_job(conn, job_id, stage="external", message="Checking the web…")
                from .external.check import run_external_for_saved_rules, run_external_mentions
                await run_external_mentions(conn, source_id)
                await run_external_for_saved_rules(conn, source_id)

        fixed = reconcile_fixed(conn, source_id, CATEGORIES, run_start)
        found = conn.execute(
            "SELECT COUNT(*) c FROM issues WHERE source_id=? AND detected_at>=? AND status='open'",
            (source_id, run_start),
        ).fetchone()["c"]
        update_job(conn, job_id, status="done", stage="done", finished_at=now_iso(),
                   message="Sync complete.", issues_found=found, issues_fixed=fixed)
    except asyncio.CancelledError:
        update_job(conn, job_id, status="canceled", cancelled=1, finished_at=now_iso(),
                   message="Sync cancelled.")
        raise
    except Exception as exc:  # noqa: BLE001
        update_job(conn, job_id, status="error", finished_at=now_iso(),
                   error=str(exc), message=f"Sync failed: {exc}")


# ---- needs-sync detector (DB-based; cached by caller) ----

async def run_external_job(conn, job_id: int, source_id: int, *, discover: bool = True) -> None:
    """Staged external run: (discover) → fetch pages → brand-scope → fact-check."""
    from .external.factcheck_ext import run_external_factcheck
    from .external.pages import crawl_external_pages
    from .external.scope import scope_pages

    update_job(conn, job_id, status="running", started_at=now_iso(), stage="fetch",
               message="Fetching external pages…")
    try:
        def prog(done, total, errs):
            update_job(conn, job_id, progress=done, total=total, errors=errs)

        update_job(conn, job_id, stage="fetch", message="Fetching external pages…")
        await crawl_external_pages(conn, source_id, only_pending=True, on_progress=prog)

        update_job(conn, job_id, stage="scope", message="Checking which mentions are about us…")
        await scope_pages(conn, source_id, on_progress=prog)

        update_job(conn, job_id, stage="factcheck", message="Checking external facts…")
        res = await run_external_factcheck(conn, source_id)

        update_job(conn, job_id, status="done", stage="done", finished_at=now_iso(),
                   message="External check complete.", issues_found=res["findings"])
    except asyncio.CancelledError:
        update_job(conn, job_id, status="canceled", cancelled=1, finished_at=now_iso(),
                   message="External check cancelled.")
        raise
    except Exception as exc:  # noqa: BLE001
        update_job(conn, job_id, status="error", finished_at=now_iso(),
                   error=str(exc), message=f"External check failed: {exc}")


def needs_sync(conn, source_id: int) -> tuple[bool, str]:
    """Cheap check: never crawled, or a page's sitemap lastmod is newer than its last crawl."""
    from .config import settings

    primary = settings()["crawl"]["primary_domain"]
    total = conn.execute(
        "SELECT COUNT(*) c FROM urls WHERE source_id=? AND in_source=1 AND url LIKE ?",
        (source_id, f"%{primary}%")).fetchone()["c"]
    crawled = conn.execute(
        "SELECT COUNT(DISTINCT c.url_id) c FROM crawl_results c JOIN urls u ON u.id=c.url_id WHERE u.source_id=?",
        (source_id,)).fetchone()["c"]
    if crawled == 0:
        return True, "Never synced"
    if crawled < total:
        return True, f"{total - crawled} page(s) never synced"
    stale = conn.execute(
        """SELECT COUNT(*) c FROM urls
           WHERE source_id=? AND in_source=1 AND lastmod IS NOT NULL
             AND last_crawled IS NOT NULL AND lastmod > last_crawled""",
        (source_id,)).fetchone()["c"]
    if stale:
        return True, f"{stale} page(s) updated since last sync"
    return False, "Up to date"
