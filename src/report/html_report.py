"""Self-contained jinja2 HTML dashboard with claim inventory + hreflang grouping."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import PROJECT_ROOT, resolve_path, settings
from ..analysis.inventory import collect_inventory


def _summary(conn, source_id):
    urls = conn.execute(
        "SELECT COUNT(DISTINCT c.url_id) n FROM crawl_results c JOIN urls u ON u.id=c.url_id WHERE u.source_id=?",
        (source_id,),
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT category, severity, status, COUNT(*) n FROM issues WHERE source_id=? GROUP BY category,severity,status",
        (source_id,),
    ).fetchall()
    by_sev, by_cat = {}, {}
    open_n = fixed_n = 0
    for r in rows:
        if r["status"] == "open":
            open_n += r["n"]
            by_sev[r["severity"]] = by_sev.get(r["severity"], 0) + r["n"]
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["n"]
        elif r["status"] == "fixed":
            fixed_n += r["n"]
    return {"urls": urls, "open": open_n, "fixed": fixed_n, "by_severity": by_sev, "by_category": by_cat}


def _issues(conn, source_id):
    rows = conn.execute(
        """SELECT i.id, u.url, u.locale, u.hreflang_group_id AS hg, i.category, i.severity,
                  i.title, i.detail, i.evidence, i.expected, ru.url AS related_url,
                  i.detection_method, i.status, i.detected_at
           FROM issues i JOIN urls u ON u.id=i.url_id
           LEFT JOIN urls ru ON ru.id=i.related_url_id
           WHERE i.source_id=?""",
        (source_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _inventory(conn, source_id):
    inv = collect_inventory(conn, source_id)
    out = []
    for kind, data in inv.items():
        values = sorted(data["values"].items(), key=lambda kv: -len(kv[1]))
        if not values:
            continue
        out.append({
            "kind": kind,
            "label": data["label"],
            "category": data["category"],
            "canonical": data["canonical"],
            "values": [{"value": v, "count": len(occ), "example_url": occ[0][1]} for v, occ in values],
        })
    # facts/counts first, generic buckets last
    out.sort(key=lambda d: (d["category"] is None, d["kind"]))
    return out


def _external(conn, source_id):
    rows = conn.execute(
        """SELECT kind, fact_name, domain, external_url, title, snippet, verdict, reason, expected
           FROM external_findings WHERE source_id=? ORDER BY kind, verdict""",
        (source_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def generate_html(conn, source_id: int):
    src = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    payload = {
        "source": {"id": src["id"], "name": src["name"], "location": src["location"]},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": _summary(conn, source_id),
        "issues": _issues(conn, source_id),
        "inventory": _inventory(conn, source_id),
        "external": _external(conn, source_id),
    }
    env = Environment(
        loader=FileSystemLoader(str(PROJECT_ROOT / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    tmpl = env.get_template("report.html")
    # Safe to embed in a <script>: neutralise any "</script>" / "<!--" breakout.
    safe_json = json.dumps(payload).replace("</", "<\\/")
    html = tmpl.render(payload_json=safe_json)

    out_dir = resolve_path(settings()["paths"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out_dir / f"report_source{source_id}_{date}.html"
    path.write_text(html, encoding="utf-8")
    print(f"HTML report -> {path} ({payload['summary']['open']} open issues, "
          f"{payload['summary']['fixed']} fixed)")
    return path
