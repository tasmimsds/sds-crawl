"""Background jobs (crawl + full sync) with progress persisted in `jobs`."""
from __future__ import annotations

import asyncio

from starlette.concurrency import run_in_threadpool

from .config import settings
from .crawler import crawl_source
from .db import CATEGORIES, now_iso, reconcile_fixed, source_domain


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


def _read_stats(conn, source_id):
    """READ-stage tallies from the latest crawl result per URL."""
    primary = source_domain(conn, source_id) or settings()["crawl"]["primary_domain"]
    read = conn.execute(
        """SELECT COUNT(*) c FROM urls u JOIN crawl_results c ON c.id=(
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1)
           WHERE u.source_id=? AND c.body_text IS NOT NULL AND c.body_text!=''""",
        (source_id,)).fetchone()["c"]
    unreadable = conn.execute(
        """SELECT COUNT(*) c FROM urls u JOIN crawl_results c ON c.id=(
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1)
           WHERE u.source_id=? AND (c.body_text IS NULL OR c.body_text=''
                 OR c.error IS NOT NULL OR c.status_code>=400)""",
        (source_id,)).fetchone()["c"]
    faqs = conn.execute(
        "SELECT COUNT(*) c FROM faqs f JOIN urls u ON u.id=f.url_id WHERE u.source_id=?",
        (source_id,)).fetchone()["c"]
    return {"pages_read": read, "pages_unreadable": unreadable, "faqs_extracted": faqs}


def _match_stats(conn, source_id):
    """MATCH-stage tallies from fact_matches for this source's pages."""
    row = conn.execute(
        """SELECT
             COUNT(*) total,
             SUM(verdict='positive') pos,
             SUM(verdict='issue') iss,
             SUM(verdict='unclear') unc,
             COUNT(DISTINCT fact_rule_id) facts
           FROM fact_matches WHERE url_id IN (SELECT id FROM urls WHERE source_id=?)""",
        (source_id,)).fetchone()
    enabled = conn.execute("SELECT COUNT(*) c FROM fact_rules WHERE enabled=1").fetchone()["c"]
    return {"claims_extracted": row["total"] or 0, "matches_positive": row["pos"] or 0,
            "matches_issue": row["iss"] or 0, "matches_unclear": row["unc"] or 0,
            "facts_checked": enabled}


def _update_read_stats(conn, job_id, source_id):
    update_job(conn, job_id, **_read_stats(conn, source_id))


def _update_match_stats(conn, job_id, source_id):
    update_job(conn, job_id, **_match_stats(conn, source_id))


# maps a running job's `stage` to which of the 3 flow stages is active
_STAGE_OF = {"sitemap": 1, "pages": 1, "read": 2, "facts": 3, "external": 3, "done": 3}


def pipeline_state(conn, source_id: int) -> dict:
    """The 3-stage flow (CRAWL -> READ -> MATCH) for a source: live if a sync is
    running, else the last completed run's numbers. Shared by dashboard + live JSON."""
    primary = source_domain(conn, source_id) or settings()["crawl"]["primary_domain"]
    total = conn.execute(
        "SELECT COUNT(*) c FROM urls WHERE source_id=? AND in_source=1 AND url LIKE ?",
        (source_id, f"%{primary}%")).fetchone()["c"]
    crawled = conn.execute(
        "SELECT COUNT(DISTINCT c.url_id) c FROM crawl_results c JOIN urls u ON u.id=c.url_id WHERE u.source_id=?",
        (source_id,)).fetchone()["c"]
    errors = conn.execute(
        """SELECT COUNT(*) c FROM crawl_results c JOIN urls u ON u.id=c.url_id
           WHERE u.source_id=? AND (c.error IS NOT NULL OR c.status_code>=400)
             AND c.id=(SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1)""",
        (source_id,)).fetchone()["c"]
    running = conn.execute(
        "SELECT * FROM jobs WHERE source_id=? AND type IN ('sync','crawl') AND status IN ('running','queued') ORDER BY id DESC LIMIT 1",
        (source_id,)).fetchone()
    last = conn.execute(
        "SELECT * FROM jobs WHERE source_id=? AND type IN ('sync','crawl') AND status IN ('done','error','canceled') ORDER BY id DESC LIMIT 1",
        (source_id,)).fetchone()
    job = running or last
    active = _STAGE_OF.get(job["stage"], 0) if running and job else 0

    def status_for(idx):
        if not running:
            if last and last["status"] == "error":
                return "failed" if idx >= active else "complete"
            return "complete" if (last and crawled) else "pending"
        if idx < active:
            return "complete"
        if idx == active:
            return "running"
        return "pending"

    rd = _read_stats(conn, source_id)
    mt = _match_stats(conn, source_id)
    # live crawl progress from the running job
    prog = (running["progress"] if running else crawled) or 0
    tot = (running["total"] if running and running["total"] else total) or 0
    last_ts = last["finished_at"] if last else None
    return {
        "running": bool(running), "job_id": (running["id"] if running else (last["id"] if last else None)),
        "stage_active": active, "last_finished": last_ts,
        "error": (job["error"] if job else None),
        "scope_label": (job["scope_label"] if job and "scope_label" in job.keys() else None),
        "s1": {"name": "CRAWL", "status": status_for(1), "crawled": prog, "total": tot, "errors": errors},
        "s2": {"name": "READ", "status": status_for(2), "read": rd["pages_read"],
               "unreadable": rd["pages_unreadable"], "claims": mt["claims_extracted"],
               "faqs": rd["faqs_extracted"]},
        "s3": {"name": "FACT MATCH", "status": status_for(3), "facts": mt["facts_checked"],
               "positive": mt["matches_positive"], "issues": mt["matches_issue"],
               "unclear": mt["matches_unclear"]},
        "message": (job["message"] if job else ""),
    }


# external flow stages: DISCOVER -> FETCH -> SCOPE -> READ -> FACT MATCH
_EXT_STAGE_OF = {"discover": 1, "fetch": 2, "scope": 3, "read": 4, "factcheck": 5, "done": 5}


def external_pipeline_state(conn, source_id: int) -> dict:
    """5-stage EXTERNAL flow (DISCOVER -> FETCH -> SCOPE -> READ -> FACT MATCH) for a
    project's brand: live if an external run is going, else last run's numbers. Strictly
    scoped to THIS project (its brand profile + its external_pages)."""
    from .external.brand import get_brand
    from .external import dataforseo as _dfs
    brand = get_brand(conn, source_id)
    brand_name = (brand or {}).get("brand_name") or ""

    one = lambda q, *a: conn.execute(q, a).fetchone()["c"]
    # DISCOVER — DataForSEO backlinks + mentions + manual sources
    backlinks = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND source_type='backlink'", source_id)
    # 'discovery' is the legacy SERP-mention type — count it with mentions
    mentions = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND source_type IN ('mention','discovery')", source_id)
    linked = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND mention_type='linked'", source_id)
    unlinked = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND mention_type='unlinked'", source_id)
    deferred = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='deferred'", source_id)
    # TRUE totals from the last discovery (whole footprint, not the processed subset)
    from .db import get_setting
    gi = lambda k: int(get_setting(conn, f"{k}:{source_id}") or 0)
    true_backlinks = gi("ext_true_backlinks")
    true_domains = gi("ext_true_domains")
    search_mentions = gi("ext_search_mentions")
    manual = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND source_type='manual'", source_id)
    candidates = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='candidate'", source_id)
    ready = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status IN ('pending','ok','blocked','error')", source_id)
    total_sources = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=?", source_id)
    referring_domains = one("SELECT COUNT(DISTINCT referring_domain) c FROM external_backlinks WHERE source_id=?", source_id)
    general_open = one("SELECT COUNT(*) c FROM general_facts WHERE source_id=? AND status='open'", source_id)
    # FETCH — fetch outcomes
    fok = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='ok'", source_id)
    fblocked = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='blocked'", source_id)
    ferror = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='error'", source_id)
    fpending = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='pending'", source_id)
    # SCOPE / READ — brand relevance of snippets
    snip_total = one("SELECT COUNT(*) c FROM external_snippets WHERE source_id=?", source_id)
    snip_kept = one("SELECT COUNT(*) c FROM external_snippets WHERE source_id=? AND about_brand=1", source_id)
    snip_disc = one("SELECT COUNT(*) c FROM external_snippets WHERE source_id=? AND about_brand=0", source_id)
    domains = one("SELECT COUNT(DISTINCT domain) c FROM external_pages WHERE source_id=? AND fetch_status='ok'", source_id)
    # FACT MATCH — issues live from external_findings; positive/unclear from the run job
    issues_live = one("SELECT COUNT(*) c FROM external_findings WHERE source_id=? AND kind='factcheck' AND status='open' AND deleted_at IS NULL", source_id)

    running = conn.execute(
        "SELECT * FROM jobs WHERE source_id=? AND type='external' AND status IN ('running','queued') ORDER BY id DESC LIMIT 1",
        (source_id,)).fetchone()
    last = conn.execute(
        "SELECT * FROM jobs WHERE source_id=? AND type='external' AND status IN ('done','error','canceled') ORDER BY id DESC LIMIT 1",
        (source_id,)).fetchone()
    job = running or last
    active = _EXT_STAGE_OF.get(job["stage"], 0) if running and job else 0
    ever_ran = bool(last)

    def status_for(idx):
        if not running:
            if last and last["status"] == "error":
                return "failed" if idx >= active else "complete"
            return "complete" if ever_ran else "pending"
        if idx < active:
            return "complete"
        if idx == active:
            return "running"
        return "pending"

    jr = job if job else None
    positive = (jr["ext_positive"] if jr and "ext_positive" in jr.keys() else 0) or 0
    unclear = (jr["ext_unclear"] if jr and "ext_unclear" in jr.keys() else 0) or 0
    issue_ct = issues_live or ((jr["ext_issue"] if jr and "ext_issue" in jr.keys() else 0) or 0)

    return {
        "running": bool(running),
        "job_id": (running["id"] if running else (last["id"] if last else None)),
        "stage_active": active, "last_finished": (last["finished_at"] if last else None),
        "error": (job["error"] if job else None),
        "brand": brand_name,
        "has_external": total_sources > 0,
        "has_brand": bool(brand_name),
        "dfs_connected": _dfs.enabled(),
        "domains": domains,
        "s1": {"name": "DISCOVER", "status": status_for(1), "backlinks": backlinks,
               "mentions": mentions, "linked": linked, "unlinked": unlinked, "manual": manual,
               "candidates": candidates, "ready": ready, "total": total_sources,
               "referring_domains": referring_domains, "deferred": deferred,
               "true_backlinks": true_backlinks, "true_domains": true_domains,
               "search_mentions": search_mentions},
        "s2": {"name": "FETCH", "status": status_for(2), "ok": fok, "blocked": fblocked,
               "errored": ferror, "pending": fpending},
        "s3": {"name": "SCOPE", "status": status_for(3), "total": snip_total,
               "kept": snip_kept, "discarded": snip_disc},
        "s4": {"name": "READ", "status": status_for(4), "claims": snip_kept},
        "s5": {"name": "MATCH & SORT", "status": status_for(5), "positive": positive,
               "issues": issue_ct, "unclear": unclear, "general": general_open},
        "message": (job["message"] if job else ""),
    }


_FACT_CATS = ("database_size", "positioning", "free_claim", "language_count", "region_count",
              "regulation_count", "feature_claim", "faq", "other_mismatch")


def _resolve_locales(conn, source_id, locale_cfg):
    """Locale include-list for crawl_source from a saved locale scope. None = all."""
    from .db import ENGLISH_PRESET, locales_for_source
    mode = (locale_cfg or {}).get("mode", "all")
    if mode == "all":
        return None
    detected = {l["code"] for l in locales_for_source(conn, source_id)}
    if mode == "english":
        allow = [c for c in ENGLISH_PRESET if c in detected]
        allow.append("(root)")  # root/default pages count as English
        return allow
    # custom
    return list((locale_cfg or {}).get("locales") or []) or None


async def run_sync_job(conn, job_id: int, source_id: int, *, only_changed=False,
                       include_external=False, scope=None, locale=None) -> None:
    """One click = check sitemap, crawl pages, then FACT MATCH (this is a fact-check-only tool).

    locale: {mode, locales} filters which URLs to crawl before fetching (defaults to the
    project's saved config). include_external also refreshes external/web findings.
    """
    from .analysis.facts import analyze_facts_regex
    from .analysis.inventory import consistency_check
    from .db import get_run_config, scope_label, set_run_config
    from .ingest import add_and_ingest

    cfg = get_run_config(conn, source_id)
    scope = scope if scope is not None else cfg["scope"]
    locale = locale if locale is not None else cfg["locale"]
    set_run_config(conn, source_id, scope, locale)  # persist as this project's default
    label = scope_label(scope, locale)
    # advanced crawl scope (Filter Builder) vs simple locale preset
    crawl_filter = None
    if (locale or {}).get("mode") == "advanced":
        crawl_filter = (locale or {}).get("filter")
        locale_allow = None
    else:
        locale_allow = _resolve_locales(conn, source_id, locale)

    run_start = now_iso()
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    update_job(conn, job_id, status="running", started_at=run_start, stage="sitemap",
               progress=0, total=0, message="Checking site map…",
               scope_label=label, run_scope=__import__("json").dumps(scope))
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

        await crawl_source(conn, source_id, only_changed=only_changed, on_progress=on_progress,
                           locales=locale_allow, crawl_filter=crawl_filter)

        after_hashes = _latest_hashes(conn, source_id)
        changed = sum(1 for uid, h in after_hashes.items()
                      if uid in before_hashes and before_hashes[uid] != h)
        update_job(conn, job_id, urls_changed=changed)

        # Stage 2b — READ: tally extraction results for the pipeline flow
        update_job(conn, job_id, stage="read", message="Reading pages…")
        _update_read_stats(conn, job_id, source_id)

        # Stage 3 — FACT MATCH: crawl content is matched against the fact rules.
        # (This tool is fact-check only — no SEO/technical/cannibalization analysis.)
        update_job(conn, job_id, stage="facts", message="Matching facts…")
        from .db import clear_matches_for_source
        await run_in_threadpool(clear_matches_for_source, conn, source_id)
        await run_in_threadpool(analyze_facts_regex, conn, source_id)
        await run_in_threadpool(consistency_check, conn, source_id)
        from .analysis.products import analyze_product_claims
        await run_in_threadpool(analyze_product_claims, conn, source_id)
        from .factcheck.scan import run_query_rules
        await run_query_rules(conn, source_id)
        reconcile_cats = list(_FACT_CATS)
        # detectors that ran deterministically (their issues may be reconciled)
        ran_methods = ["regex", "inventory", "context", "llm"]

        # AI screening pass — the deepest fact check (paraphrase/nuance). Part of a full
        # fact check; only trust it for reconciliation if it completed (didn't pause on caps).
        from .analysis.fact_check import fact_check_llm
        screen = await fact_check_llm(conn, source_id, all_locales=True)
        if isinstance(screen, dict) and screen.get("completed"):
            ran_methods.append("ai_screen")

        # FAQ extraction + check is part of fact checking (always on)
        from .analysis.faqs import analyze_faqs
        await analyze_faqs(conn, source_id)

        _update_match_stats(conn, job_id, source_id)

        # Stage 4 (scheduled runs) — external / web findings
        if include_external:
            from .config import serp_enabled
            if serp_enabled():
                update_job(conn, job_id, stage="external", message="Checking the web…")
                from .external.check import run_external_for_saved_rules, run_external_mentions
                await run_external_mentions(conn, source_id)
                await run_external_for_saved_rules(conn, source_id)

        # Only reconcile issues from detectors that actually ran this run (detector-aware):
        # never mark AI-screen findings 'fixed' when the screening pass didn't complete.
        fixed = reconcile_fixed(conn, source_id, reconcile_cats, run_start,
                                methods=ran_methods) if reconcile_cats else 0
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
    """Staged external run mirroring the dashboard flow:
    DISCOVER → FETCH → SCOPE → READ → FACT MATCH. Scoped to this project's brand."""
    from .external.discover import discover_external
    from .external.factcheck_ext import run_external_factcheck
    from .external.pages import crawl_external_pages
    from .external.scope import scope_pages

    update_job(conn, job_id, status="running", started_at=now_iso(), stage="discover",
               message="Discovering backlinks & brand mentions (DataForSEO)…")
    try:
        def prog(done, total, errs):
            update_job(conn, job_id, progress=done, total=total, errors=errs)

        # 1) DISCOVER — DataForSEO backlinks + brand mentions (capped + cached).
        if discover:
            try:
                d = await run_in_threadpool(discover_external, conn, source_id)
                update_job(conn, job_id, message=(
                    f"Discovered {d.get('true_backlinks',0)} backlinks · "
                    f"{d.get('true_referring_domains',0)} domains · "
                    f"{d.get('search_mentions',0)} search mentions"
                    + (f" (cost ${d['cost']})" if d.get("cost") else "")
                    + (" — cached" if d.get("cached") else "")))
            except Exception as exc:  # noqa: BLE001 — discovery is best-effort
                print(f"  ! discovery skipped: {exc}")

        # 2) FETCH — pull the approved/manual pending pages
        update_job(conn, job_id, stage="fetch", message="Fetching external pages…")
        await crawl_external_pages(conn, source_id, only_pending=True, on_progress=prog)

        # 3) SCOPE — keep only passages specifically about THIS brand
        update_job(conn, job_id, stage="scope", message="Checking which mentions are about us…")
        await scope_pages(conn, source_id, on_progress=prog)

        # 4) READ — the brand-relevant passages are the extracted claims (display stage)
        update_job(conn, job_id, stage="read", message="Extracting brand claims…")

        # 5) FACT MATCH — run fact rules against brand snippets
        update_job(conn, job_id, stage="factcheck", message="Checking external facts…")
        res = await run_external_factcheck(conn, source_id)

        update_job(conn, job_id, status="done", stage="done", finished_at=now_iso(),
                   message="External check complete.", issues_found=res["findings"],
                   ext_positive=res.get("positive", 0), ext_issue=res.get("findings", 0),
                   ext_unclear=res.get("unclear", 0), ext_general=res.get("general", 0))
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

    primary = source_domain(conn, source_id) or settings()["crawl"]["primary_domain"]
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
