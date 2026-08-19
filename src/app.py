"""FastAPI web app — fact-checking first, Site Health secondary.

Run:  python -m src.app     (-> http://localhost:8000)
"""
from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .config import PROJECT_ROOT, openrouter_key, resolve_path, settings
from .db import connect, get_products, source_domain
from .ingest import add_and_ingest
from .jobs import create_job, get_job, needs_sync, run_crawl_job, run_sync_job

app = FastAPI(title="SDS Fact Check")
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="static")
_out = resolve_path(settings()["paths"]["output_dir"])
_out.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=str(_out)), name="output")

_tasks: dict[int, asyncio.Task] = {}
_needs_cache: dict[int, tuple[float, bool, str]] = {}  # source_id -> (ts, needs, reason)


def _reltime(iso: str | None) -> str:
    from datetime import datetime, timezone

    if not iso:
        return "never"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = (datetime.now(timezone.utc) - t).total_seconds()
    if secs < 60:
        return "just now"
    for unit, n in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= n:
            v = int(secs // n)
            return f"{v} {unit}{'s' if v != 1 else ''} ago"
    return "just now"


def _fmt_dur(start: str | None, end: str | None) -> str:
    from datetime import datetime

    if not start or not end:
        return "—"
    try:
        d = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except ValueError:
        return "—"
    if d < 60:
        return f"{int(d)}s"
    return f"{int(d // 60)}m {int(d % 60)}s"


def _fromjson(v):
    import json
    try:
        return json.loads(v) if v else []
    except (json.JSONDecodeError, TypeError):
        return []


_CAT_LABELS = {
    "database_size": "Database size", "positioning": "Positioning",
    "free_claim": "Free-plan claim", "language_count": "Language count",
    "region_count": "Region count", "regulation_count": "Regulation count",
    "feature_claim": "Feature claim", "faq": "FAQ",
    "other_mismatch": "Other mismatch", "external_mismatch": "External mismatch",
}


def _cat_label(cat):
    return _CAT_LABELS.get(cat, (cat or "").replace("_", " ").capitalize())


def _short_url(u, maxlen: int = 46):
    """Host-dropped, middle-truncated URL for display (full URL goes in title)."""
    import re

    if not u:
        return ""
    s = re.sub(r"^https?://(www\.)?", "", u).rstrip("/")
    if len(s) <= maxlen:
        return s
    keep = maxlen // 2 - 1
    return s[:keep] + "…" + s[-keep:]


def _pretty_title(t):
    """Turn an internal issue title/slug into human text."""
    import re

    if not t:
        return "Issue"
    t = re.sub(r"^(stale_fact:|llm(_fact)?:|rule:|fact:|query:|q_)", "", t).strip()
    if t.lower().startswith("http_"):
        return "HTTP " + t.split("_", 1)[1]
    t = t.replace(":stale", " (outdated)").replace(":inconsistent", " (inconsistent)")
    if ":" in t and "(" not in t:
        t = t.split(":", 1)[1] if len(t.split(":", 1)[1]) > 2 else t.split(":", 1)[0]
    t = t.replace("_", " ").strip()
    return (t[:1].upper() + t[1:])[:90] if t else "Issue"


def _comma(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return n


def _highlight_match(evidence, matched):
    """Escape the trimmed evidence and wrap the matched phrase in <mark> so the
    exact fact stands out. Safe: everything is HTML-escaped first; only our own
    <mark> tags are markup."""
    import html
    import re as _re
    from markupsafe import Markup

    text = html.escape(str(evidence or ""))
    m = str(matched or "").strip()
    if m:
        em = html.escape(m)
        text = _re.sub(_re.escape(em), lambda x: f"<mark>{x.group(0)}</mark>", text, count=1)
    return Markup(text)


def _highlight_external(paragraph, anchor=None, aliases=None, fact_value=None):
    """Highlight an external context paragraph with THREE distinct styles:
    the anchor text (hl-anchor), brand/alias names (hl-brand), and any matched
    fact phrase (hl-fact). Escapes first; never nests marks."""
    import html
    import re as _re
    from markupsafe import Markup

    text = html.escape(str(paragraph or ""))
    spans = []
    if fact_value:
        spans.append((str(fact_value), "hl-fact"))
    for a in (aliases or []):
        if a:
            spans.append((str(a), "hl-brand"))
    if anchor:
        spans.append((str(anchor), "hl-anchor"))
    for term, cls in spans:
        term_e = html.escape(term).strip()
        if len(term_e) < 2:
            continue
        pat = _re.compile(_re.escape(term_e), _re.I)
        parts = _re.split(r'(<mark class="[^"]*">.*?</mark>)', text)
        for i, seg in enumerate(parts):
            if seg.startswith("<mark"):
                continue
            parts[i] = pat.sub(lambda m: f'<mark class="{cls}">{m.group(0)}</mark>', seg)
        text = "".join(parts)
    return Markup(text)


templates.env.globals["reltime"] = _reltime
templates.env.globals["fmt_dur"] = _fmt_dur
templates.env.filters["fromjson"] = _fromjson
templates.env.filters["cat_label"] = _cat_label
templates.env.filters["short_url"] = _short_url
templates.env.filters["pretty_title"] = _pretty_title
templates.env.filters["comma"] = _comma
templates.env.filters["highlight_match"] = _highlight_match
templates.env.filters["highlight_external"] = _highlight_external

FACT_CATS = ("database_size", "positioning", "free_claim", "language_count",
             "region_count", "regulation_count", "feature_claim", "faq", "other_mismatch")


def _conn():
    return connect()


def _sources(conn):
    return conn.execute("SELECT * FROM sources ORDER BY id").fetchall()


def _active_source(request: Request, conn):
    srcs = _sources(conn)
    if not srcs:
        return None, srcs
    want = request.cookies.get("site_id")
    if want and want.isdigit():
        for s in srcs:
            if s["id"] == int(want):
                return s, srcs
    return srcs[0], srcs


def _count(conn, sid, cats, status="open"):
    marks = ",".join("?" for _ in cats)
    return conn.execute(
        f"SELECT COUNT(*) c FROM issues WHERE source_id=? AND status=? AND deleted_at IS NULL "
        f"AND category IN ({marks})",
        (sid, status, *cats),
    ).fetchone()["c"]


def _nav(conn, active_src, srcs):
    nav = {"sources": srcs, "active_site": active_src,
           "open_issues": 0, "failing_rules": 0}
    if active_src:
        sid = active_src["id"]
        nav["open_issues"] = _count(conn, sid, FACT_CATS)
        nav["failing_rules"] = _count(conn, sid, FACT_CATS)
    return nav


def render(request, name, active, ctx=None):
    conn = _conn()
    active_src, srcs = _active_source(request, conn)
    data = {"active": active, "nav": _nav(conn, active_src, srcs), "site": active_src}
    if ctx:
        data.update(ctx)
    return templates.TemplateResponse(request, name, data)


# ---- website switcher ----
@app.get("/switch")
def switch_site(site: int, next: str = "/"):
    resp = RedirectResponse(next or "/", status_code=303)
    resp.set_cookie("site_id", str(site), max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


# ---- Dashboard (default landing) ----
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = _conn()
    active_src, srcs = _active_source(request, conn)
    cards = []
    for s in srcs:
        sid = s["id"]
        _primary = source_domain(conn, sid) or settings()["crawl"]["primary_domain"]
        total = conn.execute(
            "SELECT COUNT(*) c FROM urls WHERE source_id=? AND in_source=1 AND url LIKE ?",
            (sid, f"%{_primary}%")).fetchone()["c"]
        crawled = conn.execute(
            "SELECT COUNT(DISTINCT c.url_id) c FROM crawl_results c JOIN urls u ON u.id=c.url_id WHERE u.source_id=?",
            (sid,)).fetchone()["c"]
        errs = conn.execute(
            """SELECT COUNT(*) c FROM crawl_results c JOIN urls u ON u.id=c.url_id
               WHERE u.source_id=? AND (c.error IS NOT NULL OR c.status_code>=400)
                 AND c.id=(SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1)""",
            (sid,)).fetchone()["c"]
        running = conn.execute(
            "SELECT * FROM jobs WHERE source_id=? AND status IN ('running','queued') ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
        last = conn.execute(
            "SELECT * FROM jobs WHERE source_id=? AND status IN ('done','error','canceled') ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
        # cached needs-sync
        c = _needs_cache.get(sid)
        if not c or time.time() - c[0] > 600:
            ns, reason = needs_sync(conn, sid)
            _needs_cache[sid] = (time.time(), ns, reason)
        else:
            ns, reason = c[1], c[2]
        if running:
            state = "syncing"
        elif last and last["status"] == "error":
            state = "failed"
        elif ns:
            state = "needs"
        else:
            state = "synced"
        sch = conn.execute("SELECT * FROM schedules WHERE source_id=?", (sid,)).fetchone()
        ext_sch = conn.execute("SELECT * FROM external_schedules WHERE source_id=?", (sid,)).fetchone()
        ext_src = conn.execute("SELECT COUNT(*) c FROM external_pages WHERE source_id=?", (sid,)).fetchone()["c"]
        ext_ok = conn.execute("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='ok'", (sid,)).fetchone()["c"]
        ext_find = conn.execute(
            "SELECT COUNT(*) c FROM external_findings WHERE source_id=? AND kind='factcheck' AND status='open'",
            (sid,)).fetchone()["c"]
        from .jobs import pipeline_state, external_pipeline_state
        from .db import get_run_config, scope_label
        pipe = pipeline_state(conn, sid)
        ext_pipe = external_pipeline_state(conn, sid)
        rc = get_run_config(conn, sid)
        # external system runs as its own job — surface its live/last state independently
        ext_running = conn.execute(
            "SELECT id FROM jobs WHERE source_id=? AND type='external' AND status IN ('running','queued') LIMIT 1",
            (sid,)).fetchone()
        ext_state = "syncing" if ext_running else ("synced" if ext_pipe["last_finished"] else "needs")
        cards.append({"row": s, "total": total, "crawled": crawled, "errors": errs,
                      "running": running, "last": last, "needs_reason": reason, "state": state,
                      "sched": sch, "ext_sched": ext_sch, "ext_state": ext_state,
                      "ext_sources": ext_src, "ext_fetched": ext_ok,
                      "ext_findings": ext_find, "pipe": pipe, "ext_pipe": ext_pipe,
                      "saved_scope": scope_label(rc["scope"], rc["locale"])})
    history = conn.execute(
        """SELECT j.*, s.name AS sname, s.location AS sloc FROM jobs j
           LEFT JOIN sources s ON s.id=j.source_id ORDER BY j.id DESC LIMIT 20""").fetchall()

    # "What changed" for the active site, from its last completed sync
    changes = None
    if active_src:
        sid = active_src["id"]
        primary = source_domain(conn, sid) or settings()["crawl"]["primary_domain"]
        last = conn.execute(
            """SELECT * FROM jobs WHERE source_id=? AND status='done' AND started_at IS NOT NULL
               ORDER BY id DESC LIMIT 1""", (sid,)).fetchone()
        if last:
            st = last["started_at"]
            new_urls = conn.execute(
                "SELECT COUNT(*) c FROM urls WHERE source_id=? AND in_source=1 AND first_seen>=? AND url LIKE ?",
                (sid, st, f"%{primary}%")).fetchone()["c"]
            removed = conn.execute(
                "SELECT COUNT(*) c FROM urls WHERE source_id=? AND in_source=0", (sid,)).fetchone()["c"]
            new_issues = conn.execute(
                """SELECT category, COUNT(*) c FROM issues WHERE source_id=? AND detected_at>=?
                   AND status='open' GROUP BY category ORDER BY c DESC""", (sid, st)).fetchall()
            changes = {"job": last, "new_urls": new_urls, "removed": removed,
                       "changed": last["urls_changed"] or 0, "new_issues": new_issues,
                       "fixed": last["issues_fixed"] or 0}
    return render(request, "dashboard.html", "dashboard",
                  {"cards": cards, "history": history, "changes": changes})


@app.get("/sites/{source_id}/matrix", response_class=HTMLResponse)
def match_matrix(request: Request, source_id: int):
    conn = _conn()
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    rows = conn.execute(
        """SELECT fr.id pk, fr.slug, fr.name, fr.correct_value, p.name product,
                  SUM(fm.verdict='positive') pos, SUM(fm.verdict='issue') iss,
                  SUM(fm.verdict='unclear') unc, COUNT(fm.id) total
           FROM fact_rules fr
           LEFT JOIN products p ON p.id=fr.product_id
           LEFT JOIN fact_matches fm ON fm.fact_rule_id=fr.id
                AND fm.url_id IN (SELECT id FROM urls WHERE source_id=?)
           WHERE fr.enabled=1
           GROUP BY fr.id ORDER BY total DESC, fr.slug""", (source_id,)).fetchall()
    # optional drill-down: one rule + verdict -> page list with quotes
    rule = request.query_params.get("rule")
    verdict = request.query_params.get("verdict")
    drill = None
    if rule and rule.isdigit() and verdict in ("positive", "issue", "unclear"):
        pages = conn.execute(
            """SELECT u.url, fm.matched_value, fm.evidence FROM fact_matches fm
               JOIN urls u ON u.id=fm.url_id
               WHERE fm.fact_rule_id=? AND fm.verdict=? AND u.source_id=?
               ORDER BY u.url LIMIT 300""", (int(rule), verdict, source_id)).fetchall()
        rname = conn.execute("SELECT name FROM fact_rules WHERE id=?", (int(rule),)).fetchone()
        drill = {"rule": int(rule), "verdict": verdict, "pages": [dict(p) for p in pages],
                 "name": rname["name"] if rname else rule}
    return render(request, "matrix.html", "results",
                  {"src": src, "rows": [dict(r) for r in rows], "drill": drill})


@app.get("/runs/{job_id}", response_class=HTMLResponse)
def run_detail(request: Request, job_id: int):
    conn = _conn()
    job = get_job(conn, job_id)
    if not job:
        return RedirectResponse("/", status_code=303)
    src = conn.execute("SELECT * FROM sources WHERE id=?", (job["source_id"],)).fetchone()
    err_pages = conn.execute(
        """SELECT u.url, c.status_code, c.error FROM urls u JOIN crawl_results c ON c.id=(
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1)
           WHERE u.source_id=? AND (c.error IS NOT NULL OR c.status_code>=400) LIMIT 200""",
        (job["source_id"],)).fetchall()
    return render(request, "run_detail.html", "dashboard",
                  {"job": job, "src": src, "err_pages": err_pages})


# ---- Fact Check ----
def _csv(s: str) -> list[str]:
    # term lists are newline-delimited so individual terms may contain commas
    # (e.g. "17,000,000"); fall back to comma-split only if there are no newlines.
    s = s or ""
    parts = s.split("\n") if "\n" in s else s.split(",")
    return [x.strip() for x in parts if x.strip()]


def _fact_from_form(f) -> dict:
    return {
        "fact_name": f.get("fact_name", "").strip(),
        "claim_topic": f.get("claim_topic", "").strip(),
        "correct_value": f.get("correct_value", "").strip() or None,
        "category": f.get("category", "other_mismatch"),
        "search_terms": _csv(f.get("search_terms", "")),
        "stale_indicators": _csv(f.get("stale_indicators", "")),
        "allowed_mentions": _csv(f.get("allowed_mentions", "")),
        "severity": f.get("severity", "high"),
        "applies_to": f.get("applies_to", "all"),
        "product_id": int(f.get("product_id")) if str(f.get("product_id") or "").strip().isdigit() else None,
        "notes": f.get("notes", ""),
    }


def _external_ctx(conn, source_id):
    from .external.brand import ensure_brand_profile
    from .config import serp_enabled

    if not source_id:
        return {}
    brand = ensure_brand_profile(conn, source_id)
    pages = conn.execute(
        "SELECT domain, url, source_type, fetch_status, fetch_error FROM external_pages WHERE source_id=? AND fetch_status!='candidate' ORDER BY domain",
        (source_id,)).fetchall()
    candidates = conn.execute(
        "SELECT id, domain, url, title FROM external_pages WHERE source_id=? AND fetch_status='candidate' ORDER BY domain",
        (source_id,)).fetchall()
    finds = conn.execute(
        """SELECT id, domain, external_url, snippet, verdict, reason, expected, finding_type, fact_rule, severity
           FROM external_findings WHERE source_id=? AND kind='factcheck' AND status='open'
                 AND deleted_at IS NULL
           ORDER BY domain""", (source_id,)).fetchall()
    by_domain = {}
    for f in finds:
        by_domain.setdefault(f["domain"] or "?", []).append(dict(f))
    running = conn.execute(
        "SELECT id FROM jobs WHERE source_id=? AND type='external' AND status IN ('running','queued') ORDER BY id DESC LIMIT 1",
        (source_id,)).fetchone()
    return {"brand": brand, "ext_pages": [dict(p) for p in pages],
            "ext_candidates": [dict(c) for c in candidates],
            "ext_findings": by_domain, "ext_finding_count": len(finds),
            "ext_running": running["id"] if running else None,
            "serp_on": serp_enabled()}


@app.get("/fact-check", response_class=HTMLResponse)
def fact_check(request: Request):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    open_facts = _count(conn, active_src["id"], FACT_CATS) if active_src else 0
    rules = conn.execute("SELECT COUNT(*) c FROM fact_rules WHERE enabled=1").fetchone()["c"]
    recent = conn.execute("SELECT * FROM queries ORDER BY last_run DESC LIMIT 8").fetchall()
    ctx = {"mode": "input", "open_facts": open_facts, "rules": rules, "recent_queries": recent}
    ctx.update(_external_ctx(conn, active_src["id"] if active_src else None))
    return render(request, "fact-check.html", "factcheck", ctx)


@app.post("/external/add-sources")
async def external_add(request: Request, urls: str = Form(""), file: UploadFile | None = File(None)):
    from .external.pages import add_external_urls

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    url_list = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
    if file is not None and file.filename:
        content = (await file.read()).decode("utf-8", "ignore")
        url_list += [ln.split(",")[0].strip() for ln in content.splitlines() if ln.strip().lower().startswith("http")]
    if url_list:
        add_external_urls(conn, active_src["id"], url_list, "manual")
    return RedirectResponse("/fact-check", status_code=303)


@app.post("/external/run")
async def external_run(source_id: int = Form(None), request: Request = None):
    from .jobs import create_job, run_external_job

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    sid = source_id or (active_src["id"] if active_src else None)
    if not sid:
        return JSONResponse({"error": "no site"}, status_code=400)
    if _has_active(conn, sid):
        return JSONResponse({"error": "A run is already in progress."}, status_code=409)
    job_id = create_job(conn, "external", sid)
    task = asyncio.create_task(run_external_job(conn, job_id, sid))
    _tasks[job_id] = task
    task.add_done_callback(lambda t: _tasks.pop(job_id, None))
    return JSONResponse({"job_id": job_id})


@app.post("/external/discover")
async def external_discover(request: Request, force: str = Form("")):
    """DataForSEO discovery (backlinks + mentions) for the active project. Returns
    counts + actual API cost; skips (cached) unless force=1."""
    from .external.discover import discover_external

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    res = await run_in_threadpool(discover_external, conn, active_src["id"],
                                  force=bool(force))
    return JSONResponse(res)


@app.post("/external/approve")
def external_approve(request: Request, ids: str = Form(""), approve_all: str = Form("")):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return RedirectResponse("/fact-check", status_code=303)
    if approve_all:
        conn.execute("UPDATE external_pages SET fetch_status='ok' WHERE source_id=? AND fetch_status='candidate'",
                     (active_src["id"],))
    else:
        for pid in [x for x in ids.split(",") if x.strip().isdigit()]:
            conn.execute("UPDATE external_pages SET fetch_status='ok' WHERE id=? AND source_id=?",
                         (int(pid), active_src["id"]))
    conn.commit()
    return RedirectResponse("/fact-check", status_code=303)


@app.post("/external/{fid}/recheck")
async def external_recheck(fid: int):
    from .findings import recheck_external_finding

    out = await run_in_threadpool(recheck_external_finding, _conn(), fid)
    return JSONResponse(out)


# ---- General Facts (external brand info that doesn't map to a defined fact) ----
# ---- Schema Checker & Suggestion Engine ----
_last_schema_batch: dict = {}


@app.get("/schema", response_class=HTMLResponse)
def schema_page(request: Request):
    from .schema_checker import google_rules, vocab
    v = vocab.load()
    return render(request, "schema_checker.html", "schema",
                  {"batch": None, "vocab_types": len(v.types), "vocab_props": len(v.properties),
                   "vocab_fetched": v.fetched_at, "ruleset": google_rules.RULESET["version"],
                   "features": sorted(google_rules.RULESET["features"])})


@app.post("/schema/check", response_class=HTMLResponse)
async def schema_check(request: Request, mode: str = Form("url"), url: str = Form(""),
                       snippet: str = Form("")):
    from .schema_checker import google_rules, service, vocab
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    sid = active_src["id"] if active_src else None
    if mode == "snippet" and snippet.strip():
        batch = {"results": [await run_in_threadpool(service.check_snippet, snippet)], "count": 1,
                 "cross_page_org": [], "ruleset_version": google_rules.RULESET["version"]}
    else:
        urls = await run_in_threadpool(service.expand_sitemap, url.strip()) if url.strip().endswith(".xml") \
            else [url.strip()]
        urls = [u for u in urls if u][:50]
        batch = await run_in_threadpool(service.check_batch, conn, urls, sid)
    _last_schema_batch[sid or 0] = batch
    v = vocab.load()
    return render(request, "schema_checker.html", "schema",
                  {"batch": batch, "single": batch["results"][0] if batch["count"] == 1 else None,
                   "vocab_types": len(v.types), "vocab_props": len(v.properties),
                   "vocab_fetched": v.fetched_at, "ruleset": google_rules.RULESET["version"],
                   "features": sorted(google_rules.RULESET["features"])})


@app.get("/schema/export.xlsx")
def schema_export_xlsx(request: Request):
    from .schema_checker.report import build_xlsx
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    batch = _last_schema_batch.get(active_src["id"] if active_src else 0)
    if not batch:
        return JSONResponse({"error": "run a check first"}, status_code=400)
    return _xlsx_response(build_xlsx(batch), "schema_check")


@app.get("/schema/export.json")
def schema_export_json(request: Request):
    from fastapi.responses import Response
    from .schema_checker.report import build_json
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    batch = _last_schema_batch.get(active_src["id"] if active_src else 0)
    if not batch:
        return JSONResponse({"error": "run a check first"}, status_code=400)
    return Response(build_json(batch), media_type="application/json",
                    headers={"content-disposition": "attachment; filename=schema_check.json"})


@app.post("/schema/refresh-vocab")
async def schema_refresh_vocab():
    from .schema_checker import vocab
    return JSONResponse(await run_in_threadpool(vocab.refresh, True))


@app.get("/general-facts", response_class=HTMLResponse)
def general_facts_page(request: Request):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return render(request, "general_facts.html", "general", {"rows": [], "external_items": [],
                      "brand": "", "aliases": [], "counts": {}, "kinds": [], "domains": []})
    sid = active_src["id"]
    qp = request.query_params
    where, params = ["source_id=?"], [sid]
    if qp.get("needs_change") in ("yes", "no", "undecided"):
        where.append("needs_change=?"); params.append(qp["needs_change"])
    if qp.get("kind"):
        where.append("source_kind=?"); params.append(qp["kind"])
    if qp.get("domain"):
        where.append("domain=?"); params.append(qp["domain"])
    if qp.get("status"):
        where.append("status=?"); params.append(qp["status"])
    else:
        where.append("status='open'")
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM general_facts WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 500",
        params)]
    counts = {r["needs_change"]: r["c"] for r in conn.execute(
        "SELECT needs_change, COUNT(*) c FROM general_facts WHERE source_id=? AND status='open' GROUP BY needs_change",
        (sid,))}
    kinds = [r["source_kind"] for r in conn.execute(
        "SELECT DISTINCT source_kind FROM general_facts WHERE source_id=? ORDER BY source_kind", (sid,))]
    domains = [r["domain"] for r in conn.execute(
        "SELECT domain, COUNT(*) c FROM general_facts WHERE source_id=? AND status='open' GROUP BY domain ORDER BY c DESC LIMIT 40", (sid,))]
    from .external.brand import get_brand
    brand = get_brand(conn, sid) or {}
    aliases = [brand.get("brand_name")] + list(brand.get("aliases", []))
    aliases = [a for a in aliases if a]
    # Backlinks & Mentions table: external items with mention_type, anchor, context paragraph
    # and verdict (mismatch ✗ / general 📋 / correct ✓ / — for discarded/pending).
    mt = request.query_params.get("mention_type")
    ew = ["p.source_id=?", "p.source_type IN ('backlink','mention')"]
    ep = [sid]
    if mt in ("linked", "unlinked"):
        ew.append("p.mention_type=?"); ep.append(mt)
    external_items = [dict(r) for r in conn.execute(
        f"""SELECT p.url AS source_url, p.domain, p.mention_type, p.anchor_text,
                   p.context_paragraph, p.fetch_status,
                   (SELECT f.expected FROM external_findings f WHERE f.page_id=p.id
                      AND f.kind='factcheck' AND f.status='open' AND f.deleted_at IS NULL LIMIT 1) AS fact_value,
                   (SELECT 1 FROM external_findings f WHERE f.page_id=p.id AND f.kind='factcheck'
                      AND f.status='open' AND f.deleted_at IS NULL LIMIT 1) AS is_mismatch,
                   (SELECT 1 FROM general_facts g WHERE g.page_id=p.id AND g.status='open' LIMIT 1) AS is_general
            FROM external_pages p
            WHERE {' AND '.join(ew)} ORDER BY p.mention_type, p.id DESC LIMIT 400""", ep)]
    vfilter = request.query_params.get("verdict")
    for it in external_items:
        it["verdict"] = ("mismatch" if it["is_mismatch"] else
                         "general" if it["is_general"] else
                         "discarded" if it["fetch_status"] == "ok" else "pending")
    if vfilter:
        external_items = [i for i in external_items if i["verdict"] == vfilter]
    return render(request, "general_facts.html", "general",
                  {"rows": rows, "counts": counts, "kinds": kinds, "domains": domains,
                   "external_items": external_items, "aliases": aliases,
                   "brand": brand.get("brand_name") or ""})


@app.get("/external-items/export.csv")
def external_items_csv(request: Request):
    from .external.brand import get_brand
    from .report.csv_export import build_external_items_csv
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    b = get_brand(conn, active_src["id"]) or {}
    aliases = [b.get("brand_name")] + list(b.get("aliases", []))
    return _csv_response(build_external_items_csv(conn, active_src["id"], aliases), "backlinks_mentions")


@app.get("/external-items/export.xlsx")
def external_items_xlsx(request: Request):
    from .external.brand import get_brand
    from .report.xlsx_export import build_external_items_xlsx
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    b = get_brand(conn, active_src["id"]) or {}
    aliases = [b.get("brand_name")] + list(b.get("aliases", []))
    return _xlsx_response(build_external_items_xlsx(conn, active_src["id"], aliases), "backlinks_mentions")


@app.get("/general-facts/export.csv")
def general_facts_csv(request: Request, scope: str = "open"):
    from .report.csv_export import build_general_facts_csv
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error":"no site"}, status_code=400)
    return _csv_response(build_general_facts_csv(conn, active_src["id"], scope), "general_facts")


@app.get("/general-facts/export.xlsx")
def general_facts_xlsx(request: Request, scope: str = "open"):
    from .report.xlsx_export import build_general_facts_xlsx
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error":"no site"}, status_code=400)
    return _xlsx_response(build_general_facts_xlsx(conn, active_src["id"], scope), "general_facts")


@app.post("/general-facts/{gid}/update")
def general_fact_update(gid: int, needs_change: str = Form(None), note: str = Form(None)):
    conn = _conn()
    if needs_change in ("yes", "no", "undecided"):
        conn.execute("UPDATE general_facts SET needs_change=? WHERE id=?", (needs_change, gid))
    if note is not None:
        conn.execute("UPDATE general_facts SET note=? WHERE id=?", (note, gid))
    conn.commit()
    return JSONResponse({"ok": True})


@app.post("/general-facts/{gid}/dismiss")
def general_fact_dismiss(gid: int):
    conn = _conn()
    conn.execute("UPDATE general_facts SET status='dismissed' WHERE id=?", (gid,))
    conn.commit()
    return JSONResponse({"ok": True})


@app.post("/general-facts/{gid}/create-issue")
def general_fact_create_issue(gid: int):
    """Promote a general fact the user judged wrong into an external issue."""
    from .db import now_iso
    conn = _conn()
    g = conn.execute("SELECT * FROM general_facts WHERE id=?", (gid,)).fetchone()
    if not g:
        return JSONResponse({"error": "not found"}, status_code=404)
    cur = conn.execute(
        """INSERT INTO external_findings (source_id, kind, external_url, domain, snippet,
             verdict, reason, finding_type, severity, status, created_at, last_checked_at)
           VALUES (?, 'factcheck', ?, ?, ?, 'mismatch', ?, 'other_mismatch', 'high', 'open', ?, ?)""",
        (g["source_id"], g["source_url"], g["domain"], g["quote"],
         "Flagged from General Facts review", now_iso(), now_iso()))
    conn.execute("UPDATE general_facts SET status='promoted', issue_id=? WHERE id=?",
                 (cur.lastrowid, gid))
    conn.commit()
    return JSONResponse({"ok": True, "issue_id": cur.lastrowid})


@app.post("/general-facts/{gid}/promote-fact")
def general_fact_promote(gid: int, current_value: str = Form(""), name: str = Form("")):
    """Turn a recurring general statement into a tracked fact rule (scope=external)."""
    from .db import now_iso
    conn = _conn()
    g = conn.execute("SELECT * FROM general_facts WHERE id=?", (gid,)).fetchone()
    if not g:
        return JSONResponse({"error": "not found"}, status_code=404)
    slug = f"genfact_{gid}"
    label = name or (g["quote"] or "")[:80]
    conn.execute(
        """INSERT INTO fact_rules (slug, name, description, rule_type, category, correct_value,
             scope, severity, applies_to, enabled, created_at, updated_at)
           VALUES (?,?,?, 'query', 'other_mismatch', ?, 'external', 'medium', 'all', 1, ?, ?)
           ON CONFLICT(slug) DO UPDATE SET name=excluded.name,
             correct_value=excluded.correct_value, description=excluded.description""",
        (slug, label, g["quote"], current_value or None, now_iso(), now_iso()))
    row = conn.execute("SELECT id FROM fact_rules WHERE slug=?", (slug,)).fetchone()
    conn.execute("UPDATE general_facts SET status='promoted', promoted_fact_id=? WHERE id=?",
                 (row["id"] if row else None, gid))
    conn.commit()
    return JSONResponse({"ok": True, "fact_id": slug})


@app.post("/fact-check/interpret", response_class=HTMLResponse)
async def fact_interpret(request: Request, text: str = Form(...),
                         severity: str = Form("high"), applies_to: str = Form("all")):
    from .factcheck.interpret import interpret

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    data = await interpret(conn, text, active_src["id"] if active_src else None)
    for fct in data["facts"]:
        fct["severity"] = severity
        fct["applies_to"] = applies_to
    products = get_products(conn, active_src["id"]) if active_src else []
    return render(request, "fact-check.html", "factcheck",
                  {"mode": "confirm", "facts": data["facts"], "vague": data["vague"],
                   "raw_text": text, "products": products})


@app.post("/fact-check/run", response_class=HTMLResponse)
async def fact_run(request: Request):
    from .factcheck.detect import run_fact

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    form = await request.form()
    fact = _fact_from_form(form)
    crawled = conn.execute(
        "SELECT COUNT(DISTINCT c.url_id) c FROM crawl_results c JOIN urls u ON u.id=c.url_id WHERE u.source_id=?",
        (active_src["id"],)).fetchone()["c"] if active_src else 0
    if not crawled:
        return render(request, "fact-check.html", "factcheck",
                      {"mode": "sync_first", "facts": [fact]})
    result = await run_fact(conn, active_src["id"], fact)
    return render(request, "fact-check.html", "factcheck", {"mode": "results", "result": result})


@app.post("/fact-check/external", response_class=HTMLResponse)
async def fact_external(request: Request, text: str = Form(...)):
    from .config import serp_enabled
    from .external.check import run_external_fact
    from .factcheck.interpret import interpret

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not serp_enabled():
        return render(request, "fact-check.html", "factcheck",
                      {"mode": "external", "ext_error": "Bright Data SERP is not configured "
                       "(set BRIGHT_DATA_* in .env).", "raw_text": text})
    data = await interpret(conn, text, active_src["id"] if active_src else None)
    fact = data["facts"][0] if data["facts"] else {"fact_name": text[:60], "search_terms": [text],
                                                    "claim_topic": text, "correct_value": None}
    result = await run_external_fact(conn, active_src["id"], fact)
    return render(request, "fact-check.html", "factcheck",
                  {"mode": "external", "ext_result": result, "raw_text": text})


@app.post("/external/mentions", response_class=HTMLResponse)
async def external_mentions_run(request: Request):
    from .config import serp_enabled
    from .external.check import run_external_mentions

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not serp_enabled():
        return render(request, "fact-check.html", "factcheck",
                      {"mode": "mentions", "ext_error": "Bright Data SERP is not configured."})
    result = await run_external_mentions(conn, active_src["id"])
    return render(request, "fact-check.html", "factcheck",
                  {"mode": "mentions", "ext_result": result})


@app.post("/fact-check/save")
async def fact_save(request: Request):
    import json as _json
    from .db import now_iso

    conn = _conn()
    form = await request.form()
    fact = _fact_from_form(form)
    slug = "fact_" + _slugify(fact["fact_name"] or (fact["search_terms"][0] if fact["search_terms"] else "fact"))
    rtype = "stale" if fact["stale_indicators"] else "query"
    pid = int(fact["product_id"]) if str(fact.get("product_id") or "").isdigit() else None
    conn.execute(
        """INSERT INTO fact_rules
             (slug,name,description,rule_type,category,correct_value,search_terms,current_patterns,
              stale_patterns,allowed_patterns,claim_patterns,require_context,context_window,
              severity,applies_to,product_id,enabled,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
           ON CONFLICT(slug) DO UPDATE SET correct_value=excluded.correct_value,
             search_terms=excluded.search_terms, stale_patterns=excluded.stale_patterns,
             allowed_patterns=excluded.allowed_patterns, category=excluded.category,
             severity=excluded.severity, product_id=excluded.product_id,
             updated_at=excluded.updated_at, enabled=1""",
        (slug, fact["fact_name"], fact["claim_topic"] or fact["fact_name"], rtype,
         fact["category"], fact["correct_value"], _json.dumps(fact["search_terms"]), "[]",
         _json.dumps(fact["stale_indicators"]), _json.dumps(fact["allowed_mentions"]), "[]",
         "[]", 120, fact["severity"], fact["applies_to"], pid, now_iso(), now_iso()),
    )
    conn.commit()
    return RedirectResponse(f"/facts?saved={slug}", status_code=303)


@app.post("/facts/save-rule")
async def save_rule(request: Request, query_text: str = Form(...), correct_value: str = Form(""),
                    terms: str = Form(""), category: str = Form("other_mismatch")):
    from .db import now_iso
    import json as _json

    conn = _conn()
    slug = "q_" + "".join(c if c.isalnum() else "_" for c in query_text.lower())[:40]
    term_list = [t.strip() for t in terms.split(",") if t.strip()]
    conn.execute(
        """INSERT INTO fact_rules
             (slug, name, description, rule_type, category, correct_value, search_terms,
              stale_patterns, allowed_patterns, claim_patterns, current_patterns,
              require_context, context_window, severity, applies_to, enabled, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
           ON CONFLICT(slug) DO UPDATE SET correct_value=excluded.correct_value,
             search_terms=excluded.search_terms, updated_at=excluded.updated_at, enabled=1""",
        (slug, query_text[:80], f"Saved from query: {query_text}", "query", category,
         correct_value or None, _json.dumps(term_list), "[]", "[]", "[]", "[]", "[]", 120,
         "high", "all", now_iso(), now_iso()),
    )
    conn.commit()
    return RedirectResponse(f"/facts?saved={slug}", status_code=303)


# ---- Results & Issues ----
def _issue_rows(conn, sid, cats, request):
    where = ["i.source_id=?", "i.deleted_at IS NULL", f"i.category IN ({','.join('?' for _ in cats)})"]
    params = [sid, *cats]
    for key, col in (("category", "i.category"), ("severity", "i.severity"),
                     ("locale", "u.locale"), ("status", "i.status")):
        v = request.query_params.get(key)
        if v:
            where.append(f"{col}=?")
            params.append(v)
    if "status" not in request.query_params:
        where.append("i.status='open'")
    prod = request.query_params.get("product")
    if prod and prod.isdigit():
        where.append("i.product_id=?")
        params.append(int(prod))
    sql = (f"""SELECT i.id,u.url,u.locale,u.content_type,u.author,i.category,i.severity,i.title,
                   i.detail,i.evidence,i.matched_value,i.expected,i.detection_method,i.status,
                   i.note,i.edited, i.last_checked_at, i.product_id, p.name AS product_name,
                   ru.url AS related_url
               FROM issues i JOIN urls u ON u.id=i.url_id
               LEFT JOIN urls ru ON ru.id=i.related_url_id
               LEFT JOIN products p ON p.id=i.product_id
               WHERE {' AND '.join(where)}
               ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END, u.url LIMIT 500""")
    return [dict(r) for r in conn.execute(sql, params)]


def _findings_from_model(conn, sid, model, limit=500):
    """Compile an Advanced Filter model to parameterized queries over issues (internal)
    and external_findings (external), selected by the model's scope toggles."""
    from .filters import EXTERNAL_FIELDS, FINDINGS_FIELDS, compile_model, scopes_of
    sc = scopes_of(model)
    groups = (model or {}).get("groups", [])
    has_status = any(g.get("field") == "status" for g in groups)
    inc_int = sc.get("internal", True)
    inc_ext = sc.get("external", False)
    if not sc:  # empty scopes -> default internal
        inc_int, inc_ext = True, False
    rows, ext_rows = [], []
    if inc_int:
        where, pr = compile_model(model, FINDINGS_FIELDS)
        base, p = ["i.source_id=?", "i.deleted_at IS NULL"], [sid]
        if not has_status:
            base.append("i.status='open'")
        if where:
            base.append(f"({where})"); p += pr
        rows = [dict(r) for r in conn.execute(
            f"""SELECT i.id,u.url,u.locale,u.content_type,u.author,i.category,i.severity,i.title,
                   i.detail,i.evidence,i.matched_value,i.expected,i.detection_method,i.status,
                   i.note,i.edited, i.last_checked_at, i.product_id, pp.name AS product_name,
                   ru.url AS related_url
               FROM issues i JOIN urls u ON u.id=i.url_id
               LEFT JOIN urls ru ON ru.id=i.related_url_id
               LEFT JOIN products pp ON pp.id=i.product_id
               WHERE {' AND '.join(base)}
               ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END, u.url LIMIT ?""", (*p, limit))]
    if inc_ext:
        where, pr = compile_model(model, EXTERNAL_FIELDS)
        base, p = ["ef.source_id=?", "ef.deleted_at IS NULL", "ef.kind='factcheck'"], [sid]
        if not has_status:
            base.append("ef.status='open'")
        if where:
            base.append(f"({where})"); p += pr
        ext_rows = [dict(r) for r in conn.execute(
            f"""SELECT ef.id,ef.domain,ef.external_url,ef.snippet,ef.verdict,ef.reason,ef.expected,
                   ef.finding_type,ef.severity,ef.status FROM external_findings ef
               WHERE {' AND '.join(base)} ORDER BY ef.domain LIMIT ?""", (*p, limit))]
    return rows, ext_rows


def _parse_filter_param(conn, request):
    """Return a filter model from ?saved=<id> or ?filter=<url-encoded JSON>, else None."""
    import json as _json
    saved = request.query_params.get("saved")
    if saved and saved.isdigit():
        from .filters import get_filter
        return get_filter(conn, int(saved))
    raw = request.query_params.get("filter")
    if raw:
        try:
            return _json.loads(raw)
        except ValueError:
            return None
    return None


@app.get("/results", response_class=HTMLResponse)
def results(request: Request):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    scope = request.query_params.get("scope", "internal")
    rows, ext_rows, locales = [], [], []
    model = _parse_filter_param(conn, request)
    if active_src:
        locales = [r["locale"] for r in conn.execute(
            "SELECT DISTINCT locale FROM urls WHERE source_id=? AND locale IS NOT NULL ORDER BY locale",
            (active_src["id"],))]
        if model is not None:  # advanced filter path
            rows, ext_rows = _findings_from_model(conn, active_src["id"], model)
        else:  # flat legacy path (tabs)
            if scope in ("internal", "all"):
                rows = _issue_rows(conn, active_src["id"], FACT_CATS, request)
            if scope in ("external", "all"):
                ext_rows = [dict(r) for r in conn.execute(
                    """SELECT id, domain, external_url, snippet, verdict, reason, expected,
                              finding_type, severity, status FROM external_findings
                       WHERE source_id=? AND kind='factcheck' AND deleted_at IS NULL AND status='open'
                       ORDER BY domain""", (active_src["id"],))]
    products = get_products(conn, active_src["id"]) if active_src else []
    from .filters import list_filters
    saved = list_filters(conn, active_src["id"], "findings") if active_src else []
    return render(request, "results.html", "results",
                  {"rows": rows, "ext_rows": ext_rows, "scope": scope, "locales": locales,
                   "products": products, "saved_filters": saved,
                   "filter_json": request.query_params.get("filter", ""),
                   "cats": FACT_CATS, "title": "Results & Issues"})


@app.get("/results/export.csv")
def results_export_csv(request: Request):
    """CSV of the current findings view — respects the active advanced filter (live, UTF-8 BOM)."""
    from .report.csv_export import build_rows_csv
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    model = _parse_filter_param(conn, request)
    if model is None:
        model = {"scopes": {"internal": True}}
    rows, ext_rows = _findings_from_model(conn, active_src["id"], model, limit=100000) if active_src else ([], [])
    return _csv_response(build_rows_csv(rows, ext_rows), "findings_filtered.csv")


@app.get("/results/export.xlsx")
def results_export_xlsx(request: Request):
    """Excel of the current filtered findings view — same rows as the CSV export."""
    from .report.xlsx_export import build_rows_xlsx
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    model = _parse_filter_param(conn, request)
    if model is None:
        model = {"scopes": {"internal": True}}
    rows, ext_rows = _findings_from_model(conn, active_src["id"], model, limit=100000) if active_src else ([], [])
    return _xlsx_response(build_rows_xlsx(rows, ext_rows), "findings_filtered.xlsx")


@app.get("/filters/count.json")
def filters_count(request: Request):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    model = _parse_filter_param(conn, request)
    if not active_src or model is None:
        return JSONResponse({"count": 0})
    rows, ext_rows = _findings_from_model(conn, active_src["id"], model, limit=100000)
    return JSONResponse({"count": len(rows) + len(ext_rows),
                         "internal": len(rows), "external": len(ext_rows)})


@app.post("/filters/save")
async def filters_save(request: Request):
    from .filters import save_filter
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    form = await request.form()
    import json as _json
    try:
        model = _json.loads(form.get("model") or "{}")
    except ValueError:
        return JSONResponse({"error": "bad model"}, status_code=400)
    name = (form.get("name") or "").strip()
    context = form.get("context") or "findings"
    if not name or not active_src:
        return JSONResponse({"error": "name required"}, status_code=400)
    save_filter(conn, active_src["id"], context, name, model)
    return JSONResponse({"ok": True})


@app.get("/filters/list.json")
def filters_list(request: Request):
    from .filters import list_filters
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    ctx = request.query_params.get("context", "findings")
    return JSONResponse({"filters": list_filters(conn, active_src["id"], ctx) if active_src else []})


@app.post("/filters/{fid}/delete")
def filters_delete(fid: int):
    from .filters import delete_filter
    delete_filter(_conn(), fid)
    return JSONResponse({"ok": True})


# ---- Facts Library (read-only list for now) ----
def _lines(s: str) -> list[str]:
    return [x.strip() for x in (s or "").splitlines() if x.strip()]


def _slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "").lower()).strip("_")[:48] or "rule"


@app.get("/facts", response_class=HTMLResponse)
def facts_library(request: Request):
    conn = _conn()
    rules = conn.execute(
        """SELECT fr.*, p.name AS product_name,
                  (SELECT COUNT(*) FROM fact_matches m WHERE m.fact_rule_id=fr.id AND m.verdict='positive') pos,
                  (SELECT COUNT(*) FROM fact_matches m WHERE m.fact_rule_id=fr.id AND m.verdict='issue') iss,
                  (SELECT COUNT(*) FROM fact_matches m WHERE m.fact_rule_id=fr.id AND m.verdict='unclear') unc
           FROM fact_rules fr
           LEFT JOIN products p ON p.id=fr.product_id ORDER BY fr.category, fr.slug""").fetchall()
    feats = conn.execute("SELECT * FROM feature_entries ORDER BY slug").fetchall()
    return render(request, "facts.html", "facts", {"rules": rules, "feats": feats})


@app.get("/facts/rule/new", response_class=HTMLResponse)
@app.get("/facts/rule/{slug}/edit", response_class=HTMLResponse)
def rule_form(request: Request, slug: str = ""):
    conn = _conn()
    rule = conn.execute("SELECT * FROM fact_rules WHERE slug=?", (slug,)).fetchone() if slug else None
    active_src, _ = _active_source(request, conn)
    products = get_products(conn, active_src["id"]) if active_src else []
    return render(request, "rule_form.html", "facts",
                  {"rule": rule, "cats": list(FACT_CATS), "products": products})


@app.post("/facts/rule/save")
def rule_save(request: Request, slug: str = Form(""), name: str = Form(...),
              description: str = Form(""), rule_type: str = Form("stale"),
              category: str = Form("other_mismatch"), correct_value: str = Form(""),
              severity: str = Form("high"), applies_to: str = Form("all"),
              product_id: str = Form(""),
              stale_patterns: str = Form(""), allowed_patterns: str = Form(""),
              claim_patterns: str = Form(""), require_context: str = Form(""),
              search_terms: str = Form("")):
    import json as _json
    from .db import now_iso

    conn = _conn()
    slug = (slug or _slugify(name))
    pid = int(product_id) if product_id.strip().isdigit() else None
    conn.execute(
        """INSERT INTO fact_rules
             (slug,name,description,rule_type,category,correct_value,search_terms,
              current_patterns,stale_patterns,allowed_patterns,claim_patterns,require_context,
              context_window,severity,applies_to,product_id,enabled,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
           ON CONFLICT(slug) DO UPDATE SET name=excluded.name,description=excluded.description,
             rule_type=excluded.rule_type,category=excluded.category,correct_value=excluded.correct_value,
             search_terms=excluded.search_terms,stale_patterns=excluded.stale_patterns,
             allowed_patterns=excluded.allowed_patterns,claim_patterns=excluded.claim_patterns,
             require_context=excluded.require_context,severity=excluded.severity,
             applies_to=excluded.applies_to,product_id=excluded.product_id,updated_at=excluded.updated_at""",
        (slug, name, description, rule_type, category, correct_value or None,
         _json.dumps(_lines(search_terms)), "[]", _json.dumps(_lines(stale_patterns)),
         _json.dumps(_lines(allowed_patterns)), _json.dumps(_lines(claim_patterns)),
         _json.dumps(_lines(require_context)), 120, severity, applies_to,
         pid, now_iso(), now_iso()),
    )
    conn.commit()
    return RedirectResponse("/facts", status_code=303)


@app.post("/facts/rule/{slug}/toggle")
def rule_toggle(slug: str):
    conn = _conn()
    conn.execute("UPDATE fact_rules SET enabled=1-enabled WHERE slug=?", (slug,))
    conn.commit()
    return RedirectResponse("/facts", status_code=303)


@app.post("/facts/rule/{slug}/delete")
def rule_delete(slug: str):
    conn = _conn()
    conn.execute("DELETE FROM fact_rules WHERE slug=?", (slug,))
    conn.commit()
    return RedirectResponse("/facts", status_code=303)


@app.get("/facts/feature/new", response_class=HTMLResponse)
@app.get("/facts/feature/{slug}/edit", response_class=HTMLResponse)
def feature_form(request: Request, slug: str = ""):
    conn = _conn()
    feat = conn.execute("SELECT * FROM feature_entries WHERE slug=?", (slug,)).fetchone() if slug else None
    return render(request, "feature_form.html", "facts", {"feat": feat})


@app.post("/facts/feature/save")
def feature_save(request: Request, slug: str = Form(""), name: str = Form(...),
                 description: str = Form(""), status: str = Form("available"),
                 notes: str = Form("")):
    from .db import now_iso

    conn = _conn()
    slug = slug or _slugify(name)
    conn.execute(
        """INSERT INTO feature_entries (slug,name,description,status,aliases,notes,enabled,created_at,updated_at)
           VALUES (?,?,?,?,?,?,1,?,?)
           ON CONFLICT(slug) DO UPDATE SET name=excluded.name,description=excluded.description,
             status=excluded.status,notes=excluded.notes,updated_at=excluded.updated_at""",
        (slug, name, description, status, "[]", notes, now_iso(), now_iso()),
    )
    conn.commit()
    return RedirectResponse("/facts", status_code=303)


@app.post("/facts/feature/{slug}/delete")
def feature_delete(slug: str):
    conn = _conn()
    conn.execute("DELETE FROM feature_entries WHERE slug=?", (slug,))
    conn.commit()
    return RedirectResponse("/facts", status_code=303)


@app.get("/facts/export")
def facts_export():
    import yaml as _yaml
    from fastapi.responses import PlainTextResponse
    from .rules import load_features, load_rules

    conn = _conn()
    data = {"facts": load_rules(conn, enabled_only=False),
            "features": load_features(conn, enabled_only=False)}
    text = _yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    return PlainTextResponse(text, headers={"content-disposition": "attachment; filename=facts_export.yaml"})


@app.post("/facts/import")
async def facts_import(request: Request, file: UploadFile = File(...)):
    import json as _json
    import yaml as _yaml
    from .db import now_iso

    conn = _conn()
    data = _yaml.safe_load((await file.read()).decode("utf-8")) or {}
    for f in data.get("facts", []):
        conn.execute(
            """INSERT INTO fact_rules (slug,name,description,rule_type,category,correct_value,
                 search_terms,current_patterns,stale_patterns,allowed_patterns,claim_patterns,
                 require_context,context_window,severity,applies_to,enabled,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
               ON CONFLICT(slug) DO UPDATE SET correct_value=excluded.correct_value,
                 stale_patterns=excluded.stale_patterns,updated_at=excluded.updated_at""",
            (f.get("id") or _slugify(f.get("description", "rule")), f.get("description", ""),
             f.get("description"), f.get("type", "stale"), f.get("category"),
             f.get("current_value") or f.get("canonical_value"),
             _json.dumps(f.get("search_terms", [])), "[]", _json.dumps(f.get("stale_patterns", [])),
             _json.dumps(f.get("allowed_patterns", [])), _json.dumps(f.get("claim_patterns", [])),
             _json.dumps(f.get("require_context", [])), f.get("context_window", 120),
             f.get("severity", "medium"), f.get("applies_to", "all"), now_iso(), now_iso()),
        )
    conn.commit()
    return RedirectResponse("/facts", status_code=303)


# ---- Reports ----
def _csv_response(text: str, filename: str):
    from fastapi.responses import Response
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(data: bytes, filename: str):
    from fastapi.responses import Response
    return Response(data, media_type=_XLSX_MIME,
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    # live per-category open counts from the DB (download links regenerate on click)
    cats = []
    if active_src:
        for cat in FACT_CATS:
            n = conn.execute("SELECT COUNT(*) c FROM issues WHERE source_id=? AND category=? "
                             "AND status='open' AND deleted_at IS NULL",
                             (active_src["id"], cat)).fetchone()["c"]
            alln = conn.execute("SELECT COUNT(*) c FROM issues WHERE source_id=? AND category=? "
                                "AND deleted_at IS NULL", (active_src["id"], cat)).fetchone()["c"]
            if alln:
                cats.append({"cat": cat, "open": n, "all": alln})
        ext_open = conn.execute("SELECT COUNT(*) c FROM external_findings WHERE source_id=? "
                                "AND kind='factcheck' AND deleted_at IS NULL AND status='open'",
                                (active_src["id"],)).fetchone()["c"]
        gen_open = conn.execute("SELECT COUNT(*) c FROM general_facts WHERE source_id=? "
                                "AND status='open'", (active_src["id"],)).fetchone()["c"]
    else:
        ext_open = gen_open = 0
    html_files = sorted((p.name for p in _out.glob("*.html")), reverse=True)[:5]
    return render(request, "reports.html", "reports",
                  {"cats": cats, "ext_open": ext_open, "gen_open": gen_open,
                   "html_files": html_files})


@app.get("/reports/download.csv")
def reports_download_csv(request: Request, category: str = "", scope: str = "open", view: str = ""):
    """Live-generated CSV (UTF-8 BOM), regenerated from the DB on every download.
    view='by_url' aggregates one row per URL; default is one row per distinct finding."""
    from .report.csv_export import build_issue_csv, build_url_summary_csv
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    scope = "all" if scope == "all" else "open"
    if view == "by_url":
        return _csv_response(build_url_summary_csv(conn, active_src["id"], scope),
                             f"findings_by_url_{scope}.csv")
    cat = category or None
    text = build_issue_csv(conn, active_src["id"], cat, scope)
    fname = (f"issues_{cat}_{scope}.csv" if cat else f"findings_all_{scope}.csv")
    return _csv_response(text, fname)


@app.get("/reports/download-external.csv")
def reports_download_external(request: Request, scope: str = "open"):
    from .report.csv_export import build_external_csv
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    scope = "all" if scope == "all" else "open"
    return _csv_response(build_external_csv(conn, active_src["id"], scope), f"external_findings_{scope}.csv")


@app.get("/reports/download.xlsx")
def reports_download_xlsx(request: Request, category: str = "", scope: str = "open", view: str = ""):
    """Live .xlsx (same rows as the CSV) — styled header/autofilter/colours/hyperlinks."""
    from .report.xlsx_export import build_issue_xlsx, build_url_summary_xlsx
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    scope = "all" if scope == "all" else "open"
    if view == "by_url":
        return _xlsx_response(build_url_summary_xlsx(conn, active_src["id"], scope),
                              f"findings_by_url_{scope}.xlsx")
    cat = category or None
    data = build_issue_xlsx(conn, active_src["id"], cat, scope)
    fname = (f"issues_{cat}_{scope}.xlsx" if cat else f"findings_all_{scope}.xlsx")
    return _xlsx_response(data, fname)


@app.get("/reports/download-external.xlsx")
def reports_download_external_xlsx(request: Request, scope: str = "open"):
    from .report.xlsx_export import build_external_xlsx
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    scope = "all" if scope == "all" else "open"
    return _xlsx_response(build_external_xlsx(conn, active_src["id"], scope), f"external_findings_{scope}.xlsx")


@app.get("/reports/full.xlsx")
def reports_full_xlsx(request: Request, scope: str = "open"):
    """Single multi-sheet workbook: Summary tab + one sheet per category."""
    from .report.xlsx_export import build_full_report_xlsx
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    scope = "all" if scope == "all" else "open"
    return _xlsx_response(build_full_report_xlsx(conn, active_src["id"], scope),
                          f"full_report_{scope}.xlsx")


@app.post("/reports/export")
async def reports_export(request: Request):
    """Generate a fresh timestamped HTML report (CSVs download live via /reports/download.csv)."""
    from .report.html_report import generate_html
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    path = await run_in_threadpool(generate_html, conn, active_src["id"])
    return JSONResponse({"html": "/output/" + path.name})


# ---- Settings ----
def _estimate_scan_cost(conn, sid, fast_id, reasoning_id) -> dict | None:
    """Rough $/full-scan for the selected pair, based on the last run's page count.
    Screening runs ~1 call/page on the fast model; verification ~15% of pages on reasoning."""
    from .openrouter import prices_for
    if not sid:
        return None
    last = conn.execute(
        "SELECT pages_read FROM jobs WHERE source_id=? AND type='sync' AND pages_read>0 "
        "ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    pages = (last["pages_read"] if last else 0) or conn.execute(
        "SELECT COUNT(DISTINCT url_id) c FROM crawl_results c JOIN urls u ON u.id=c.url_id "
        "WHERE u.source_id=?", (sid,)).fetchone()["c"]
    if not pages:
        return None
    pr = prices_for([fast_id, reasoning_id])
    f, r = pr.get(fast_id, {}), pr.get(reasoning_id, {})
    # rough per-page token assumptions (observed ~2.6k prompt / 80 completion for screening)
    screen = pages * (2600 * f.get("prompt_m", 0) + 80 * f.get("completion_m", 0)) / 1_000_000
    verify = pages * 0.15 * (2600 * r.get("prompt_m", 0) + 300 * r.get("completion_m", 0)) / 1_000_000
    return {"pages": pages, "usd": round(screen + verify, 2)}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = settings()["llm"]
    import os

    from .config import CURATED_MODELS, curated_ids, serp_enabled
    from .db import get_model_config
    from .openrouter import prices_for
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    mc = get_model_config(conn)
    fast_sel = os.getenv("SDS_FAST_MODEL") or mc["fast_model"]
    reasoning_sel = os.getenv("SDS_REASONING_MODEL") or mc["reasoning_model"]
    prices = prices_for(curated_ids("fast") + curated_ids("reasoning"))

    def cards(tier, selected):
        out = []
        for m in CURATED_MODELS[tier]:
            out.append({**m, "price": prices.get(m["id"], {}), "selected": m["id"] == selected})
        return out

    fast_cards = cards("fast", fast_sel)
    reasoning_cards = cards("reasoning", reasoning_sel)
    curated_all = curated_ids("fast") + curated_ids("reasoning")
    info = {
        "fast_model": fast_sel, "reasoning_model": reasoning_sel,
        "interpret_model": mc["interpret_model"], "spend_cap": mc["spend_cap_usd"],
        "max_calls": s["max_calls_per_run"], "key_present": bool(openrouter_key()),
        "serp_on": serp_enabled(), "crawl": settings()["crawl"],
        "env_override": bool(os.getenv("SDS_FAST_MODEL")),
        # custom (advanced) models = a stored id not in the curated shortlist
        "fast_custom": fast_sel if fast_sel not in curated_all else "",
        "reasoning_custom": reasoning_sel if reasoning_sel not in curated_all else "",
        "cost": _estimate_scan_cost(conn, active_src["id"] if active_src else None,
                                    fast_sel, reasoning_sel),
    }
    brand = None
    if active_src:
        from .external.brand import ensure_brand_profile
        brand = ensure_brand_profile(conn, active_src["id"])
    active = {"id": active_src["id"], "name": active_src["name"] or active_src["location"],
              "kind": active_src["kind"], "location": active_src["location"]} if active_src else None
    return render(request, "settings.html", "settings",
                  {"info": info, "brand": brand, "fast_cards": fast_cards,
                   "reasoning_cards": reasoning_cards, "active": active})


@app.post("/settings/models")
def settings_models(fast_model: str = Form(""), reasoning_model: str = Form(""),
                    fast_custom: str = Form(""), reasoning_custom: str = Form(""),
                    spend_cap: str = Form("")):
    from .db import set_setting
    from .openrouter import validate_id
    conn = _conn()
    # a custom (Advanced) id overrides the radio selection
    fast = (fast_custom.strip() or fast_model.strip())
    reasoning = (reasoning_custom.strip() or reasoning_model.strip())
    if fast:
        set_setting(conn, "fast_model", validate_id(fast)[0])
    if reasoning:
        rid = validate_id(reasoning)[0]
        set_setting(conn, "reasoning_model", rid)
        set_setting(conn, "interpret_model", rid)  # interpretation = verification model
    try:
        if spend_cap.strip():
            set_setting(conn, "spend_cap_usd", float(spend_cap))
    except ValueError:
        pass
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/test-models")
async def settings_test_models():
    """Fire a tiny live request at each configured model; report OK / error per tier."""
    from .analysis.llm import LlmClient
    from .db import get_model_config
    conn = _conn()
    if not openrouter_key():
        return JSONResponse({"error": "No OpenRouter API key configured."}, status_code=400)
    mc = get_model_config(conn)
    llm = LlmClient(conn)
    out = {}
    for tier, model in (("fast", mc["fast_model"]), ("reasoning", mc["reasoning_model"])):
        try:
            r = await llm._client.chat.completions.create(
                model=model, max_tokens=5, temperature=0,
                messages=[{"role": "user", "content": "Reply with the word OK."}])
            txt = (r.choices[0].message.content or "").strip()
            out[tier] = {"model": model, "ok": True, "reply": txt[:40]}
        except Exception as exc:  # noqa: BLE001
            out[tier] = {"model": model, "ok": False, "error": str(exc)[:200]}
    return JSONResponse(out)


@app.get("/sites/{source_id}/delete-info.json")
def delete_info(source_id: int):
    from .db import project_delete_summary
    return JSONResponse(project_delete_summary(_conn(), source_id))


@app.post("/sites/{source_id}/delete")
async def delete_project_route(request: Request, source_id: int):
    from .db import delete_project, project_delete_summary
    conn = _conn()
    summ = project_delete_summary(conn, source_id)
    form = await request.form()
    if (form.get("confirm") or "").strip() != summ["name"]:
        return JSONResponse({"error": "Type the exact project name to confirm."}, status_code=400)
    # 1. cancel any running/queued sync for this project first
    for jid, task in list(_tasks.items()):
        j = get_job(conn, jid)
        if j and j["source_id"] == source_id:
            task.cancel()
            _tasks.pop(jid, None)
    conn.execute("UPDATE jobs SET status='canceled' WHERE source_id=? AND status IN ('running','queued')",
                 (source_id,))
    conn.commit()
    # 2. delete everything in a transaction + orphan check
    orphans = delete_project(conn, source_id)
    _needs_cache.pop(source_id, None)
    ok = all(v == 0 for v in orphans.values())
    return JSONResponse({"ok": ok, "orphans": orphans})


@app.post("/sites/{source_id}/redetect")
async def redetect_sitemap(source_id: int):
    """Re-run sitemap auto-discovery against the stored domain and replace the URL set."""
    from .ingest import add_and_ingest
    conn = _conn()
    if _has_active(conn, source_id):
        return JSONResponse({"error": "A sync is running; wait for it to finish."}, status_code=409)
    src = conn.execute("SELECT location, name FROM sources WHERE id=?", (source_id,)).fetchone()
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        stats = await run_in_threadpool(add_and_ingest, conn, src["location"], src["name"])
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Discovery failed: {exc}"}, status_code=500)
    return JSONResponse({"ok": True, "urls": stats.get("on_domain", stats.get("total_urls", 0))})


@app.post("/settings/brand")
def settings_brand(request: Request, brand_name: str = Form(...), aliases: str = Form(""),
                   own_domains: str = Form(""), disambiguation_notes: str = Form(""),
                   negative_terms: str = Form("")):
    from .external.brand import set_brand

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if active_src:
        set_brand(conn, active_src["id"], brand_name.strip(),
                  _csv(aliases), _csv(own_domains), disambiguation_notes.strip(), _csv(negative_terms))
    return RedirectResponse("/settings", status_code=303)


# ---- Add website (onboarding) ----
@app.get("/add-site", response_class=HTMLResponse)
def add_site_form(request: Request):
    return render(request, "onboarding.html", "add", {"first_run": False})


@app.post("/add-site")
async def add_site(request: Request, url: str = Form(""), name: str = Form(""),
                   file: UploadFile | None = File(None)):
    conn = _conn()
    ref = url.strip()
    if file is not None and file.filename:
        updir = resolve_path("data/uploads")
        updir.mkdir(parents=True, exist_ok=True)
        dest = updir / file.filename
        dest.write_bytes(await file.read())
        ref = str(dest)
    if not ref:
        return render(request, "onboarding.html", "add",
                      {"first_run": False, "error": "Enter a website / sitemap URL, or choose a file."})
    try:
        result = await run_in_threadpool(add_and_ingest, conn, ref, name.strip() or None)
    except Exception as exc:  # noqa: BLE001
        return render(request, "onboarding.html", "add",
                      {"first_run": False, "error": f"Could not add source: {exc}"})
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("site_id", str(result["source_id"]), max_age=60 * 60 * 24 * 365,
                    httponly=True, samesite="lax")
    return resp


# ---- sync / crawl jobs ----
def _has_active(conn, sid):
    return conn.execute(
        "SELECT id FROM jobs WHERE source_id=? AND status IN ('running','queued')", (sid,)).fetchone()


def _scope_from_form(form) -> dict:
    # This tool is fact-check only: every crawl reads content + runs fact matching (incl. FAQ).
    return {"fact_check": 1, "faq": 1}


def _locale_from_form(form) -> dict:
    import json as _json
    mode = form.get("locale_mode", "all")
    if mode == "advanced":
        try:
            flt = _json.loads(form.get("crawl_filter") or "{}")
        except ValueError:
            flt = {}
        return {"mode": "advanced", "filter": flt}
    if mode == "custom":
        return {"mode": "custom", "locales": form.getlist("locales")}
    return {"mode": mode, "locales": []}


@app.get("/sites/{source_id}/scope-count.json")
def scope_count(source_id: int, request: Request):
    """Live URL count for an advanced crawl-scope filter (before fetching)."""
    from .filters import CRAWL_FIELDS, compile_model
    import json as _json
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) c FROM urls WHERE source_id=? AND in_source=1",
                         (source_id,)).fetchone()["c"]
    try:
        model = _json.loads(request.query_params.get("filter") or "{}")
    except ValueError:
        model = {}
    where, params = compile_model(model, CRAWL_FIELDS)
    sql = "SELECT COUNT(*) c FROM urls u WHERE source_id=? AND in_source=1"
    p = [source_id]
    if where:
        sql += f" AND ({where})"; p += params
    sel = conn.execute(sql, p).fetchone()["c"]
    return JSONResponse({"selected": sel, "total": total})


@app.get("/sites/{source_id}/scope-info.json")
def scope_info(source_id: int):
    from .db import ENGLISH_PRESET, get_run_config, has_locale_structure, locales_for_source
    conn = _conn()
    locales = locales_for_source(conn, source_id)
    detected = {l["code"] for l in locales}
    total = sum(l["count"] for l in locales)
    sections = [{"code": r["s"], "count": r["c"]} for r in conn.execute(
        "SELECT COALESCE(section,'(root)') s, COUNT(*) c FROM urls WHERE source_id=? AND in_source=1 "
        "GROUP BY COALESCE(section,'(root)') ORDER BY c DESC LIMIT 40", (source_id,))]
    english = [c for c in ENGLISH_PRESET if c in detected]
    # LLM only touches English + root pages; those drive the cost estimate
    llm_codes = set(english) | {"(root)"}
    llm_total = sum(l["count"] for l in locales if l["code"] in llm_codes)
    return JSONResponse({
        "locales": locales, "sections": sections, "total": total, "english": english,
        "english_llm_total": llm_total, "has_locale": has_locale_structure(conn, source_id),
        "saved": get_run_config(conn, source_id),
        # rough per-LLM-page $ (screening + verify + faq)
        "rate": {"fact": 0.0041, "full": 0.0137}, "sec_per_page": 0.7, "concurrency": 8,
    })


@app.post("/sites/{source_id}/sync")
async def start_sync(request: Request, source_id: int):
    from .db import set_run_config
    conn = _conn()
    if _has_active(conn, source_id):
        return JSONResponse({"error": "A sync is already running for this website."}, status_code=409)
    form = await request.form()
    only_changed = form.get("only_changed") in ("1", "true", "on", "True")
    scope = _scope_from_form(form)
    locale = _locale_from_form(form)
    set_run_config(conn, source_id, scope, locale)  # persist as the project default
    job_id = create_job(conn, "sync", source_id)
    task = asyncio.create_task(run_sync_job(conn, job_id, source_id, only_changed=only_changed,
                                            scope=scope, locale=locale))
    _tasks[job_id] = task
    task.add_done_callback(lambda t: _tasks.pop(job_id, None))
    _needs_cache.pop(source_id, None)
    return JSONResponse({"job_id": job_id})


@app.post("/sites/{source_id}/crawl")
async def start_crawl(source_id: int, only_changed: bool = Form(False)):
    conn = _conn()
    job_id = create_job(conn, "crawl", source_id)
    task = asyncio.create_task(run_crawl_job(conn, job_id, source_id, only_changed=only_changed))
    _tasks[job_id] = task
    task.add_done_callback(lambda t: _tasks.pop(job_id, None))
    return JSONResponse({"job_id": job_id})


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    task = _tasks.get(job_id)
    if task:
        task.cancel()
    from .jobs import update_job
    update_job(_conn(), job_id, cancelled=1)
    return JSONResponse({"ok": True})


@app.get("/sites/{source_id}/pipeline.json")
def pipeline_json(source_id: int):
    from .jobs import pipeline_state
    return JSONResponse(pipeline_state(_conn(), source_id))


@app.get("/sites/{source_id}/external-pipeline.json")
def external_pipeline_json(source_id: int):
    from .jobs import external_pipeline_state
    return JSONResponse(external_pipeline_state(_conn(), source_id))


@app.get("/sites/{source_id}/external-scope.json")
def external_scope_json(source_id: int):
    """Pre-run preview + LLM cost estimate for an external check (SCOPE + MATCH both
    call the LLM, roughly one fast-model pass per page)."""
    from .config import serp_enabled
    from .external.brand import get_brand
    conn = _conn()
    one = lambda q, *a: conn.execute(q, a).fetchone()["c"]
    pending = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='pending'", source_id)
    ok_pages = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='ok'", source_id)
    candidates = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='candidate'", source_id)
    brand = get_brand(conn, source_id) or {}
    from .external.discover import discovery_status
    ds = discovery_status(conn, source_id)
    deferred = one("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='deferred'", source_id)
    # SCOPE + MATCH run the LLM over fetched pages (~fast-model calls); rough per-page cost.
    pages_scoped = pending + ok_pages
    per_page = 0.0009  # ~2 fast-model passes (scope + match) per page, haiku pricing
    est_llm = pages_scoped * per_page
    # discovery bills only if not cached: DataForSEO backlinks list (~$0.06/1k) + Bright Data SERP
    est_dfs = 0.0 if ds["cached"] else 0.15
    return JSONResponse({
        "brand": brand.get("brand_name") or "",
        "has_brand": bool(brand.get("brand_name")),
        "pending_to_fetch": pending, "already_fetched": ok_pages, "deferred": deferred,
        "discovery_candidates": candidates, "pages_scoped": pages_scoped,
        "discover_enabled": serp_enabled(),
        "dataforseo_connected": ds["dataforseo"], "brightdata_connected": ds["brightdata"],
        "discovery_cached": ds["cached"], "cache_hours": ds["cache_hours"],
        "last_discovery_cost": ds["last_cost"], "process_cap": ds["process_cap"],
        "true_backlinks": ds["true_backlinks"], "true_referring_domains": ds["true_referring_domains"],
        "search_mentions": ds["search_mentions"],
        "est_discovery_cost_usd": round(est_dfs, 3),
        "est_llm_cost_usd": round(est_llm, 4),
        "est_cost_usd": round(est_llm + est_dfs, 3),
    })


@app.get("/jobs/{job_id}.json")
def job_status(job_id: int):
    job = get_job(_conn(), job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    pct = int(100 * job["progress"] / job["total"]) if job["total"] else 0
    return JSONResponse({
        "id": job["id"], "status": job["status"], "stage": job["stage"] or "",
        "progress": job["progress"], "total": job["total"], "errors": job["errors"],
        "pct": pct, "message": job["message"] or "", "error": job["error"] or "",
    })


@app.post("/sites/{source_id}/schedule")
def set_site_schedule(source_id: int, mode: str = Form("off"), day_of_week: int = Form(0),
                      hour: int = Form(3), minute: int = Form(0)):
    from .scheduler import set_schedule

    set_schedule(source_id, mode, int(day_of_week), int(hour), int(minute))
    return RedirectResponse("/", status_code=303)


@app.post("/sites/{source_id}/external-schedule")
def set_site_external_schedule(source_id: int, mode: str = Form("off"), day_of_week: int = Form(0),
                               hour: int = Form(4), minute: int = Form(0)):
    from .scheduler import set_external_schedule

    set_external_schedule(source_id, mode, int(day_of_week), int(hour), int(minute))
    return RedirectResponse("/", status_code=303)


@app.on_event("startup")
async def _startup():
    from .scheduler import start_and_load

    start_and_load()


@app.post("/issues/{issue_id}/mark")
def issue_mark(issue_id: int, status: str = Form(...)):
    if status not in ("open", "fixed", "ignored"):
        return JSONResponse({"error": "bad status"}, status_code=400)
    conn = _conn()
    conn.execute("UPDATE issues SET status=? WHERE id=?", (status, issue_id))
    conn.commit()
    return JSONResponse({"ok": True, "status": status})


@app.get("/issues/{issue_id}/context")
def issue_context(issue_id: int):
    """On-demand full paragraph around the fact, pulled from the STORED page body
    (no re-crawl). Locates the matched phrase (else the trimmed evidence) in the
    latest crawl_results.body_text and returns a wide window."""
    from .util import context_around
    conn = _conn()
    row = conn.execute(
        """SELECT i.url_id, i.matched_value, i.evidence, c.body_text
             FROM issues i
             JOIN crawl_results c ON c.id = (
               SELECT id FROM crawl_results WHERE url_id=i.url_id ORDER BY id DESC LIMIT 1)
            WHERE i.id=?""", (issue_id,)).fetchone()
    if not row or not row["body_text"]:
        return JSONResponse({"context": None, "error": "no stored page body"}, status_code=404)
    body = row["body_text"]
    needle = (row["matched_value"] or "").strip()
    idx = body.find(needle) if needle else -1
    if idx < 0:  # fall back to the trimmed evidence's core phrase
        core = (row["evidence"] or "").strip("…").strip()
        probe = core[:60]
        idx = body.find(probe) if probe else -1
        needle = probe if idx >= 0 else ""
    if idx < 0:
        return JSONResponse({"context": row["evidence"], "matched": needle or None})
    ctx = context_around(body, idx, len(needle) or 1, radius=400)
    return JSONResponse({"context": ctx, "matched": needle or None})


@app.post("/issues/{issue_id}/recheck")
async def issue_recheck(issue_id: int):
    from .findings import recheck_issue

    out = await run_in_threadpool(recheck_issue, _conn(), issue_id)
    return JSONResponse(out)


@app.post("/issues/recheck-section")
async def issues_recheck_section(request: Request, category: str = Form(""),
                                 status: str = Form("open"), limit: int = Form(50)):
    """Bulk recheck a section (bounded; polite — sequential per page)."""
    from .findings import recheck_issue

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    where = ["source_id=?", "deleted_at IS NULL", "status=?"]
    params = [active_src["id"], status]
    if category:
        where.append("category=?")
        params.append(category)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM issues WHERE {' AND '.join(where)} LIMIT ?", (*params, limit))]
    fixed = still = unver = 0
    for iid in ids:
        out = await run_in_threadpool(recheck_issue, conn, iid)
        fixed += out.get("status") == "fixed"
        still += out.get("status") == "open"
        unver += out.get("status") == "unverifiable"
    return JSONResponse({"checked": len(ids), "fixed": fixed, "still_open": still, "unverifiable": unver})


@app.post("/issues/{issue_id}/edit")
def issue_edit(request: Request, issue_id: int, severity: str = Form(None),
               category: str = Form(None), expected: str = Form(None), note: str = Form(None)):
    from .findings import edit_issue

    edit_issue(_conn(), issue_id, severity or None, category or None, expected or None, note or None)
    return RedirectResponse(request.headers.get("referer", "/results"), status_code=303)


@app.post("/issues/{issue_id}/false-positive")
def issue_false_positive(issue_id: int, reason: str = Form(""), dont_flag: str = Form(""),
                         phrase: str = Form("")):
    from .findings import false_positive

    out = false_positive(_conn(), issue_id, reason, bool(dont_flag), phrase)
    return JSONResponse(out)


@app.post("/issues/{issue_id}/delete")
def issue_delete(issue_id: int):
    from .findings import delete_issue

    delete_issue(_conn(), issue_id)
    return JSONResponse({"ok": True})


@app.post("/external/{fid}/mark")
def external_mark(fid: int, action: str = Form(...)):
    from .db import now_iso

    conn = _conn()
    if action == "delete":
        conn.execute("UPDATE external_findings SET deleted_at=? WHERE id=?", (now_iso(), fid))
    elif action in ("fixed", "false_positive", "ignored", "open"):
        conn.execute("UPDATE external_findings SET status=?, last_checked_at=? WHERE id=?",
                     (action, now_iso(), fid))
    else:
        return JSONResponse({"error": "bad action"}, status_code=400)
    conn.commit()
    return JSONResponse({"ok": True})


@app.get("/healthz")
def healthz():
    return {"ok": True}


def main():
    import uvicorn

    uvicorn.run("src.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
