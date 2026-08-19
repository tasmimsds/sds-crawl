"""EXTERNAL discovery v2 — start from DataForSEO, not just manual URLs.

For the active project's domain + brand, pull (1) backlinks / referring pages and
(2) brand mentions, dedupe, exclude own domains, and register them as external_pages
(source set for the run). Manual URLs still merge in. Costs are capped per run and
cached for a window so re-runs don't re-bill.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import dataforseo
from ..db import get_setting, now_iso, set_setting, source_domain
from . import brightdata as bdserp
from . import dataforseo as dfs
from .brand import ensure_brand_profile


def _hours_since(iso: str | None) -> float:
    if not iso:
        return 1e9
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 1e9


def discovery_status(conn, source_id: int) -> dict:
    """Non-billing status for the UI: connected?, last run, cached?, TRUE totals."""
    cfg = dataforseo()
    last_at = get_setting(conn, f"ext_disc_at:{source_id}")
    age = _hours_since(last_at)
    gi = lambda k: int(get_setting(conn, f"{k}:{source_id}") or 0)
    return {
        "dataforseo": dfs.enabled(), "brightdata": bdserp.enabled(),
        "connected": dfs.enabled() or bdserp.enabled(),
        "last_discovered_at": last_at,
        "cached": age < cfg["cache_hours"],
        "cache_hours": cfg["cache_hours"],
        "last_cost": float(get_setting(conn, f"ext_disc_cost:{source_id}") or 0),
        "process_cap": cfg["process_cap"],
        # TRUE totals from the last discovery (for "3,412 backlinks · 214 domains …")
        "true_backlinks": gi("ext_true_backlinks"),
        "true_referring_domains": gi("ext_true_domains"),
        "search_mentions": gi("ext_search_mentions"),
    }


def discover_external(conn, source_id: int, *, force: bool = False,
                      process_cap: int | None = None) -> dict:
    """FULL external discovery for this project:
      • ALL backlinks (DataForSEO, fully paginated — every referring domain)
      • search-engine mentions (Bright Data Google + Bing, paginated past page 1)
    Merged, deduped by URL, own domains excluded, tagged linked/unlinked. Pulls the whole
    LIST always (cheap) but caps the expensive DOWNSTREAM fetch/match at process_cap.
    Cached within the window so re-runs don't re-bill. Returns TRUE totals + selection."""
    cfg = dataforseo()
    process_cap = process_cap or cfg["process_cap"]
    if not (dfs.enabled() or bdserp.enabled()):
        return {"connected": False, "cost": 0.0, "cached": False,
                "note": "Neither DataForSEO nor Bright Data is connected"}

    last_at = get_setting(conn, f"ext_disc_at:{source_id}")
    if not force and _hours_since(last_at) < cfg["cache_hours"]:
        return {"connected": True, "cached": True, "cost": 0.0, "last_discovered_at": last_at,
                "note": f"Using cached discovery (< {cfg['cache_hours']}h old)."}

    brand = ensure_brand_profile(conn, source_id)
    own = {d.replace("www.", "") for d in (brand.get("own_domains") or [])}
    domain = source_domain(conn, source_id) or (next(iter(own), "") if own else "")
    if domain:
        own.add(domain.replace("www.", ""))

    total_cost = 0.0
    _MIN_CTX = 80  # thin context → fetch page for the real paragraph
    true_backlinks = true_domains = 0
    linked_urls: set[str] = set()

    # 1) BACKLINKS (linked) — FULLY PAGINATED via DataForSEO. Summary gives cheap true
    #    totals; the paginated pull gives every external referring domain.
    backlinks_stored = 0
    if domain and dfs.enabled():
        try:
            summ = dfs.backlinks_summary(domain)
            total_cost += summ["cost"]
            true_backlinks, true_domains = summ["backlinks"], summ["referring_domains"]
            bl = dfs.fetch_backlinks(domain, exclude_domains=own)  # ALL external domains
            total_cost += bl["cost"]
            for it in bl["items"]:
                linked_urls.add(it["url"])
                ctx = it.get("context") or ""
                status = "ok" if len(ctx) >= _MIN_CTX else "pending"
                conn.execute(
                    """INSERT INTO external_backlinks
                         (source_id, referring_url, referring_domain, anchor, dofollow,
                          first_seen, page_title, context_paragraph, discovered_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id, referring_url) DO UPDATE SET
                         anchor=excluded.anchor, dofollow=excluded.dofollow,
                         first_seen=excluded.first_seen, page_title=excluded.page_title,
                         context_paragraph=excluded.context_paragraph""",
                    (source_id, it["url"], it["domain"], it["anchor"], 1 if it["dofollow"] else 0,
                     it["first_seen"], it["title"], ctx, now_iso()))
                cur = conn.execute(
                    """INSERT INTO external_pages (source_id, url, domain, source_type, mention_type,
                         anchor_text, context_paragraph, fetch_status, created_at)
                       VALUES (?,?,?, 'backlink', 'linked', ?, ?, ?, ?)
                       ON CONFLICT(source_id, url) DO UPDATE SET
                         mention_type='linked', anchor_text=excluded.anchor_text,
                         context_paragraph=COALESCE(NULLIF(external_pages.context_paragraph,''),excluded.context_paragraph),
                         fetch_status=CASE WHEN LENGTH(COALESCE(external_pages.context_paragraph,''))<? OR LENGTH(?)<?
                                           THEN 'pending' ELSE external_pages.fetch_status END""",
                    (source_id, it["url"], it["domain"], it["anchor"], ctx, status, now_iso(),
                     _MIN_CTX, ctx, _MIN_CTX))
                backlinks_stored += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ! backlinks discovery failed: {exc}")

    # 2) SEARCH MENTIONS — Bright Data Google + Bing, paginated. Pages that name us
    #    (may or may not link us). Unlinked = named but not in the backlink set.
    search_mentions = 0
    per_engine: dict = {}
    bn = brand["brand_name"]
    aliases = [a for a in brand.get("aliases", []) if a and a.lower() != bn.lower()][:3]
    queries = [bn, f"{bn} review", f"{bn} pricing", f"{bn} vs", f"{bn} alternative", *aliases]
    if bdserp.enabled():
        try:
            m = bdserp.discover_mentions(queries, exclude_domains=own)
            per_engine = m.get("per_engine", {})
            for it in m["items"]:
                mtype = "linked" if it["url"] in linked_urls else "unlinked"
                cur = conn.execute(
                    """INSERT INTO external_pages (source_id, url, domain, source_type, mention_type,
                         context_paragraph, fetch_status, title, created_at)
                       VALUES (?,?,?, 'mention', ?, ?, 'pending', ?, ?)
                       ON CONFLICT(source_id, url) DO UPDATE SET
                         mention_type=CASE WHEN external_pages.mention_type='linked' THEN 'linked' ELSE excluded.mention_type END,
                         context_paragraph=COALESCE(NULLIF(external_pages.context_paragraph,''),excluded.context_paragraph)""",
                    (source_id, it["url"], it["domain"], mtype, it["snippet"], it["title"], now_iso()))
                search_mentions += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Bright Data mention discovery failed: {exc}")

    # legacy SERP-discovered pages that aren't backlinks -> unlinked
    conn.execute(
        """UPDATE external_pages SET mention_type='unlinked'
           WHERE source_id=? AND source_type IN ('mention','discovery') AND mention_type IS NULL""",
        (source_id,))

    # 3) VOLUME CONTROL — pull everything, but only PROCESS (fetch/scope/match) up to the
    #    cap this run. Defer the excess pending rows (linked/backlinks prioritised).
    pending = conn.execute(
        "SELECT id FROM external_pages WHERE source_id=? AND fetch_status='pending' "
        "ORDER BY CASE mention_type WHEN 'linked' THEN 0 ELSE 1 END, id DESC",
        (source_id,)).fetchall()
    if len(pending) > process_cap:
        defer = [r["id"] for r in pending[process_cap:]]
        conn.executemany("UPDATE external_pages SET fetch_status='deferred' WHERE id=?",
                         [(i,) for i in defer])

    unique_urls = conn.execute(
        "SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND source_type IN ('backlink','mention','discovery')",
        (source_id,)).fetchone()["c"]
    selected = conn.execute(
        "SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status IN ('pending','ok')",
        (source_id,)).fetchone()["c"]

    conn.commit()
    set_setting(conn, f"ext_disc_at:{source_id}", now_iso())
    set_setting(conn, f"ext_disc_cost:{source_id}", round(total_cost, 4))
    set_setting(conn, f"ext_true_backlinks:{source_id}", true_backlinks)
    set_setting(conn, f"ext_true_domains:{source_id}", true_domains)
    set_setting(conn, f"ext_search_mentions:{source_id}", search_mentions)
    return {"connected": True, "cached": False,
            "backlinks": backlinks_stored, "true_backlinks": true_backlinks,
            "true_referring_domains": true_domains,
            "search_mentions": search_mentions, "per_engine": per_engine,
            "unique_urls": unique_urls, "selected_to_process": min(selected, process_cap + 10),
            "process_cap": process_cap, "cost": round(total_cost, 4), "domain": domain}
