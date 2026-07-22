"""One CSV per issue category in output/."""
from __future__ import annotations

import csv

from ..config import resolve_path, settings
from ..db import CATEGORIES

_HEADERS = [
    "url", "locale", "hreflang_group", "category", "severity", "title", "detail",
    "evidence", "expected", "related_url", "detection_method", "status", "detected_at",
]


def export_csv(conn, source_id: int):
    out_dir = resolve_path(settings()["paths"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for cat in CATEGORIES:
        rows = conn.execute(
            """SELECT u.url, u.locale, u.hreflang_group_id AS hg, i.category, i.severity,
                      i.title, i.detail, i.evidence, i.expected, ru.url AS related_url,
                      i.detection_method, i.status, i.detected_at
               FROM issues i JOIN urls u ON u.id=i.url_id
               LEFT JOIN urls ru ON ru.id=i.related_url_id
               WHERE i.source_id=? AND i.category=?
               ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2 ELSE 3 END, u.url""",
            (source_id, cat),
        ).fetchall()
        if not rows:
            continue
        path = out_dir / f"issues_{cat}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(_HEADERS)
            for r in rows:
                w.writerow([
                    r["url"], r["locale"] or "", r["hg"] or "", r["category"], r["severity"],
                    r["title"], r["detail"] or "", r["evidence"] or "", r["expected"] or "",
                    r["related_url"] or "", r["detection_method"] or "", r["status"],
                    r["detected_at"] or "",
                ])
        files.append(str(path))
        print(f"  wrote {len(rows)} rows -> {path}")

    # external findings (SERP-based) get their own CSV
    ext = conn.execute(
        """SELECT kind, fact_name, domain, external_url, title, snippet, verdict, reason,
                  expected, status, created_at
           FROM external_findings WHERE source_id=? ORDER BY kind, verdict""",
        (source_id,),
    ).fetchall()
    if ext:
        path = out_dir / "external_findings.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["kind", "fact", "domain", "external_url", "title", "snippet",
                        "verdict", "reason", "expected", "status", "found_at"])
            for r in ext:
                w.writerow([r["kind"], r["fact_name"] or "", r["domain"] or "", r["external_url"],
                            r["title"] or "", r["snippet"] or "", r["verdict"] or "",
                            r["reason"] or "", r["expected"] or "", r["status"], r["created_at"]])
        files.append(str(path))
        print(f"  wrote {len(ext)} external findings -> {path}")

    if not files:
        print("No issues to export.")
    return files
