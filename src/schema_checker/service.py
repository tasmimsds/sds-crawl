"""Orchestrator — Input Handler (component 1) + Fetcher/Renderer (component 2) wiring the
nine services into one per-page result. Reuses ExactFact's extractor for visible-content
parsing and its httpx fetch settings (render-before-extract via the same crawler stack).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from ..config import settings
from ..extractor import extract as page_extract
from . import extract as sd_extract
from . import generate, google_rules, intent, recommend, score, validate_vocab, vocab


def _fetch(url: str) -> dict:
    """Fetch rendered HTML + reproducibility metadata (reuses the crawler's UA/timeout)."""
    c = settings()["crawl"]
    meta = {"url": url, "final_url": url, "status": None, "fetched_at":
            datetime.now(timezone.utc).isoformat(timespec="seconds"), "html": "", "error": None}
    try:
        with httpx.Client(follow_redirects=True, timeout=c["request_timeout_s"],
                          headers={"user-agent": c["user_agent"]}) as client:
            r = client.get(url)
            meta["status"] = r.status_code
            meta["final_url"] = str(r.url)
            if r.status_code < 400 and "html" in r.headers.get("content-type", ""):
                meta["html"] = r.text
            else:
                meta["error"] = f"HTTP {r.status_code} / non-HTML"
    except Exception as exc:  # noqa: BLE001
        meta["error"] = f"{type(exc).__name__}: {exc}"
    return meta


def _brand(conn, source_id):
    try:
        from ..external.brand import get_brand
        return get_brand(conn, source_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def _severity_split(findings, key="severity"):
    errs = [f for f in findings if f[key] == "error"]
    warns = [f for f in findings if f[key] == "warning"]
    return errs, warns


def _assemble(url, html, extracted, page, meta, conn=None, source_id=None) -> dict:
    """Run the analysis chain over already-fetched html + extracted schema."""
    v = vocab.load()
    vfindings = validate_vocab.validate(extracted["nodes"], v)
    gres = google_rules.check(extracted["nodes"])
    ia = intent.analyze(html, url, page)
    recs = recommend.recommend(ia["intent"], ia["signals"], extracted["types"], vfindings)
    brand = _brand(conn, source_id) if conn and source_id else {}
    gen = generate.generate(url, page, ia["signals"], extracted, recs, brand)

    v_errs, v_warns = _severity_split(vfindings)
    validity = score.validity_score(vfindings, extracted["parse_errors"])
    coverage = score.coverage_score(recs, bool(extracted["types"]))
    prio = score.priority(validity, coverage, recs)
    return {
        "url": url, "final_url": meta.get("final_url"), "status": meta.get("status"),
        "fetched_at": meta.get("fetched_at"), "error": meta.get("error"),
        "page_title": getattr(page, "title", "") or "",
        "intent": ia["intent"], "intent_confidence": ia["confidence"],
        "signals": ia["signals"],
        # extraction (exhaustive)
        "schema_types": extracted["types"], "block_count": extracted["block_count"],
        "blocks": extracted["blocks"], "parse_errors": extracted["parse_errors"],
        "has_microdata": extracted["has_microdata"], "has_rdfa": extracted["has_rdfa"],
        # two separate validation layers
        "schemaorg_errors": v_errs, "schemaorg_warnings": v_warns,
        "google": gres,
        # advisor
        "recommendations": recs, "generated": gen,
        "org_warnings": gen["org_warnings"],
        # scores
        "validity": validity, "coverage": coverage, "priority": prio,
    }


def check_url(conn, url: str, source_id=None) -> dict:
    meta = _fetch(url)
    if meta["error"] and not meta["html"]:
        return {"url": url, "error": meta["error"], "status": meta.get("status"),
                "priority": "HIGH", "validity": 0, "coverage": 0, "schema_types": [],
                "recommendations": [], "intent": "other"}
    page = page_extract(meta["html"], url)
    extracted = sd_extract.extract(meta["html"])
    return _assemble(url, meta["html"], extracted, page, meta, conn, source_id)


def check_snippet(text: str, url: str = "") -> dict:
    """Snippet mode — validate + score pasted JSON-LD/HTML without fetching (pre-publish)."""
    extracted = sd_extract.extract_snippet(text)
    # if the snippet was a full HTML fragment we can still read visible content
    html = text if text.strip()[:1] not in ("{", "[") else ""
    page = page_extract(html, url) if html else _EmptyPage()
    meta = {"final_url": url, "status": "snippet", "fetched_at":
            datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return _assemble(url or "(snippet)", html, extracted, page, meta)


class _EmptyPage:
    title = ""
    h1s: list = []
    h2s: list = []
    body_text = ""
    faqs: list = []


def check_batch(conn, urls: list[str], source_id=None) -> dict:
    """Batch: per-page results + cross-page Organization consistency check."""
    results = [check_url(conn, u, source_id) for u in urls if u.strip()]
    # cross-page Organization name/logo consistency (batch-only signal)
    names, logos = {}, {}
    for r in results:
        for n in r.get("blocks", []):
            pass
    org_names = set()
    org_logos = set()
    for r in results:
        for node in [b.get("parsed") for b in r.get("blocks", []) if b.get("parsed")]:
            for o in _iter_orgs(node):
                if o.get("name"):
                    org_names.add(o["name"].strip())
                lg = o.get("logo")
                lg = lg.get("url") if isinstance(lg, dict) else lg
                if lg:
                    org_logos.add(lg)
    cross = []
    if len(org_names) > 1:
        cross.append(f"Organization NAME differs across pages: {sorted(org_names)} — pick one canonical.")
    if len(org_logos) > 1:
        cross.append(f"Organization LOGO differs across pages: {sorted(org_logos)}.")
    return {"results": results, "cross_page_org": cross,
            "count": len(results), "ruleset_version": google_rules.RULESET["version"]}


def _iter_orgs(obj):
    if isinstance(obj, list):
        for v in obj:
            yield from _iter_orgs(v)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        t = t if isinstance(t, list) else [t]
        if "Organization" in t or any(str(x).endswith("Organization") for x in t if x):
            yield obj
        for k, v in obj.items():
            if k not in ("@type",):
                yield from _iter_orgs(v)


def expand_sitemap(url: str, limit: int = 50) -> list[str]:
    """Expand a sitemap.xml (or return the single URL). Locale-aware ordering left to caller."""
    if not url.endswith(".xml"):
        return [url]
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            import re
            xml = client.get(url).text
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
            return locs[:limit]
    except Exception:  # noqa: BLE001
        return [url]
