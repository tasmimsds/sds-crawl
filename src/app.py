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
from .db import connect
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
    "status": "Status", "crawl": "Crawl health", "seo_technical": "Technical SEO",
    "database_size": "Database size", "positioning": "Positioning",
    "free_claim": "Free-plan claim", "language_count": "Language count",
    "region_count": "Region count", "feature_claim": "Feature claim", "faq": "FAQ",
    "cannibalization": "Cannibalization", "other_mismatch": "Other mismatch",
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


templates.env.globals["reltime"] = _reltime
templates.env.globals["fmt_dur"] = _fmt_dur
templates.env.filters["fromjson"] = _fromjson
templates.env.filters["cat_label"] = _cat_label
templates.env.filters["short_url"] = _short_url
templates.env.filters["pretty_title"] = _pretty_title
templates.env.filters["comma"] = _comma

FACT_CATS = ("database_size", "positioning", "free_claim", "language_count",
             "region_count", "feature_claim", "faq", "other_mismatch")
HEALTH_CATS = ("status", "crawl", "seo_technical", "cannibalization")


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
           "open_issues": 0, "failing_rules": 0, "health_errors": 0}
    if active_src:
        sid = active_src["id"]
        nav["open_issues"] = _count(conn, sid, FACT_CATS + HEALTH_CATS)
        nav["failing_rules"] = _count(conn, sid, FACT_CATS)
        nav["health_errors"] = _count(conn, sid, HEALTH_CATS)
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
        _primary = settings()["crawl"]["primary_domain"]
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
        ext_src = conn.execute("SELECT COUNT(*) c FROM external_pages WHERE source_id=?", (sid,)).fetchone()["c"]
        ext_ok = conn.execute("SELECT COUNT(*) c FROM external_pages WHERE source_id=? AND fetch_status='ok'", (sid,)).fetchone()["c"]
        ext_find = conn.execute(
            "SELECT COUNT(*) c FROM external_findings WHERE source_id=? AND kind='factcheck' AND status='open'",
            (sid,)).fetchone()["c"]
        cards.append({"row": s, "total": total, "crawled": crawled, "errors": errs,
                      "running": running, "last": last, "needs_reason": reason, "state": state,
                      "sched": sch, "ext_sources": ext_src, "ext_fetched": ext_ok,
                      "ext_findings": ext_find})
    history = conn.execute(
        """SELECT j.*, s.name AS sname, s.location AS sloc FROM jobs j
           LEFT JOIN sources s ON s.id=j.source_id ORDER BY j.id DESC LIMIT 20""").fetchall()

    # "What changed" for the active site, from its last completed sync
    changes = None
    if active_src:
        sid = active_src["id"]
        primary = settings()["crawl"]["primary_domain"]
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
async def external_discover(request: Request):
    from .config import serp_enabled
    from .external.pages import discover_mentions

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if active_src and serp_enabled():
        await run_in_threadpool(discover_mentions, conn, active_src["id"])
    return RedirectResponse("/fact-check", status_code=303)


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


@app.post("/fact-check/interpret", response_class=HTMLResponse)
async def fact_interpret(request: Request, text: str = Form(...),
                         severity: str = Form("high"), applies_to: str = Form("all")):
    from .factcheck.interpret import interpret

    conn = _conn()
    data = await interpret(conn, text)
    for fct in data["facts"]:
        fct["severity"] = severity
        fct["applies_to"] = applies_to
    return render(request, "fact-check.html", "factcheck",
                  {"mode": "confirm", "facts": data["facts"], "vague": data["vague"],
                   "raw_text": text})


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
    data = await interpret(conn, text)
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
    conn.execute(
        """INSERT INTO fact_rules
             (slug,name,description,rule_type,category,correct_value,search_terms,current_patterns,
              stale_patterns,allowed_patterns,claim_patterns,require_context,context_window,
              severity,applies_to,enabled,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
           ON CONFLICT(slug) DO UPDATE SET correct_value=excluded.correct_value,
             search_terms=excluded.search_terms, stale_patterns=excluded.stale_patterns,
             allowed_patterns=excluded.allowed_patterns, category=excluded.category,
             severity=excluded.severity, updated_at=excluded.updated_at, enabled=1""",
        (slug, fact["fact_name"], fact["claim_topic"] or fact["fact_name"], rtype,
         fact["category"], fact["correct_value"], _json.dumps(fact["search_terms"]), "[]",
         _json.dumps(fact["stale_indicators"]), _json.dumps(fact["allowed_mentions"]), "[]",
         "[]", 120, fact["severity"], fact["applies_to"], now_iso(), now_iso()),
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
    sql = (f"""SELECT i.id,u.url,u.locale,i.category,i.severity,i.title,i.detail,i.evidence,
                   i.expected,i.detection_method,i.status,i.note,i.edited,i.last_checked_at,
                   ru.url AS related_url
               FROM issues i JOIN urls u ON u.id=i.url_id
               LEFT JOIN urls ru ON ru.id=i.related_url_id
               WHERE {' AND '.join(where)}
               ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END, u.url LIMIT 500""")
    return [dict(r) for r in conn.execute(sql, params)]


@app.get("/results", response_class=HTMLResponse)
def results(request: Request):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    scope = request.query_params.get("scope", "internal")
    rows, ext_rows, locales = [], [], []
    if active_src:
        if scope in ("internal", "all"):
            rows = _issue_rows(conn, active_src["id"], FACT_CATS + HEALTH_CATS, request)
            locales = [r["locale"] for r in conn.execute(
                "SELECT DISTINCT locale FROM urls WHERE source_id=? AND locale IS NOT NULL ORDER BY locale",
                (active_src["id"],))]
        if scope in ("external", "all"):
            ext_rows = [dict(r) for r in conn.execute(
                """SELECT id, domain, external_url, snippet, verdict, reason, expected,
                          finding_type, severity, status FROM external_findings
                   WHERE source_id=? AND kind='factcheck' AND deleted_at IS NULL AND status='open'
                   ORDER BY domain""", (active_src["id"],))]
    return render(request, "results.html", "results",
                  {"rows": rows, "ext_rows": ext_rows, "scope": scope, "locales": locales,
                   "cats": FACT_CATS + HEALTH_CATS, "title": "Results & Issues"})


@app.get("/site-health", response_class=HTMLResponse)
def site_health(request: Request):
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    rows = _issue_rows(conn, active_src["id"], HEALTH_CATS, request) if active_src else []
    locales = []
    return render(request, "results.html", "health",
                  {"rows": rows, "locales": locales, "cats": HEALTH_CATS,
                   "title": "Site Health"})


# ---- Facts Library (read-only list for now) ----
def _lines(s: str) -> list[str]:
    return [x.strip() for x in (s or "").splitlines() if x.strip()]


def _slugify(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "").lower()).strip("_")[:48] or "rule"


@app.get("/facts", response_class=HTMLResponse)
def facts_library(request: Request):
    conn = _conn()
    rules = conn.execute("SELECT * FROM fact_rules ORDER BY category, slug").fetchall()
    feats = conn.execute("SELECT * FROM feature_entries ORDER BY slug").fetchall()
    return render(request, "facts.html", "facts", {"rules": rules, "feats": feats})


@app.get("/facts/rule/new", response_class=HTMLResponse)
@app.get("/facts/rule/{slug}/edit", response_class=HTMLResponse)
def rule_form(request: Request, slug: str = ""):
    conn = _conn()
    rule = conn.execute("SELECT * FROM fact_rules WHERE slug=?", (slug,)).fetchone() if slug else None
    return render(request, "rule_form.html", "facts", {"rule": rule, "cats": list(FACT_CATS)})


@app.post("/facts/rule/save")
def rule_save(request: Request, slug: str = Form(""), name: str = Form(...),
              description: str = Form(""), rule_type: str = Form("stale"),
              category: str = Form("other_mismatch"), correct_value: str = Form(""),
              severity: str = Form("high"), applies_to: str = Form("all"),
              stale_patterns: str = Form(""), allowed_patterns: str = Form(""),
              claim_patterns: str = Form(""), require_context: str = Form(""),
              search_terms: str = Form("")):
    import json as _json
    from .db import now_iso

    conn = _conn()
    slug = (slug or _slugify(name))
    conn.execute(
        """INSERT INTO fact_rules
             (slug,name,description,rule_type,category,correct_value,search_terms,
              current_patterns,stale_patterns,allowed_patterns,claim_patterns,require_context,
              context_window,severity,applies_to,enabled,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
           ON CONFLICT(slug) DO UPDATE SET name=excluded.name,description=excluded.description,
             rule_type=excluded.rule_type,category=excluded.category,correct_value=excluded.correct_value,
             search_terms=excluded.search_terms,stale_patterns=excluded.stale_patterns,
             allowed_patterns=excluded.allowed_patterns,claim_patterns=excluded.claim_patterns,
             require_context=excluded.require_context,severity=excluded.severity,
             applies_to=excluded.applies_to,updated_at=excluded.updated_at""",
        (slug, name, description, rule_type, category, correct_value or None,
         _json.dumps(_lines(search_terms)), "[]", _json.dumps(_lines(stale_patterns)),
         _json.dumps(_lines(allowed_patterns)), _json.dumps(_lines(claim_patterns)),
         _json.dumps(_lines(require_context)), 120, severity, applies_to, now_iso(), now_iso()),
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
@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    conn = _conn()
    files = sorted((p.name for p in _out.glob("*.html")), reverse=True)[:5]
    csvs = sorted(p.name for p in _out.glob("*.csv"))
    return render(request, "reports.html", "reports", {"html_files": files, "csv_files": csvs})


@app.post("/reports/export")
async def reports_export(request: Request):
    from .report.csv_export import export_csv
    from .report.html_report import generate_html

    conn = _conn()
    active_src, _ = _active_source(request, conn)
    if not active_src:
        return JSONResponse({"error": "no site"}, status_code=400)
    await run_in_threadpool(export_csv, conn, active_src["id"])
    path = await run_in_threadpool(generate_html, conn, active_src["id"])
    return JSONResponse({"html": "/output/" + path.name})


# ---- Settings ----
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = settings()["llm"]
    import os

    from .config import serp_enabled
    conn = _conn()
    active_src, _ = _active_source(request, conn)
    info = {
        "fast_model": os.getenv("SDS_FAST_MODEL") or s["fast_model"],
        "reasoning_model": os.getenv("SDS_REASONING_MODEL") or s["reasoning_model"],
        "max_calls": s["max_calls_per_run"], "max_output": s.get("max_output_tokens"),
        "key_present": bool(openrouter_key()), "serp_on": serp_enabled(),
        "crawl": settings()["crawl"],
    }
    brand = None
    if active_src:
        from .external.brand import ensure_brand_profile
        brand = ensure_brand_profile(conn, active_src["id"])
    return render(request, "settings.html", "settings", {"info": info, "brand": brand})


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


@app.post("/sites/{source_id}/sync")
async def start_sync(source_id: int, only_changed: bool = Form(False)):
    conn = _conn()
    if _has_active(conn, source_id):
        return JSONResponse({"error": "A sync is already running for this website."}, status_code=409)
    job_id = create_job(conn, "sync", source_id)
    task = asyncio.create_task(run_sync_job(conn, job_id, source_id, only_changed=only_changed))
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
