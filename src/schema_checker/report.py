"""Scorer & Reporter (component 9) — export via ExactFact's existing XLSX writer + JSON.

One row per page with the spec's columns. Reuses report.xlsx_export._write_sheet so schema
reports get the same header/freeze/autofilter/wrap styling as every other export.
"""
from __future__ import annotations

import io
import json

COLS = ["Page", "URL", "Page Intent", "Current Schema Types", "Current Schema Code",
        "Validity", "Coverage", "schema.org errors", "schema.org warnings",
        "Google eligibility", "Recommendation", "Generated JSON-LD", "Priority"]


def _row(r: dict) -> dict:
    verbatim = "\n\n".join(b["raw"] for b in r.get("blocks", []) if b.get("raw"))
    sorg_err = "\n".join(f"[{f['code']}] {f['message']} @ {f['path']}" for f in r.get("schemaorg_errors", []))
    sorg_warn = "\n".join(f"[{f['code']}] {f['message']} @ {f['path']}" for f in r.get("schemaorg_warnings", []))
    goog = "\n".join(
        f"{feat}: {'QUALIFIES' if fr['qualifies'] else 'NOT eligible'} — "
        + "; ".join(f"{x['severity']}:{x['property']}" for x in fr["findings"]) if fr["findings"]
        else f"{feat}: QUALIFIES"
        for feat, fr in r.get("google", {}).get("features", {}).items())
    recs = "\n".join(f"{x['action']} {x['type']} — {x['rationale']} [{x.get('priority','')}]"
                     for x in r.get("recommendations", []))
    return {
        "Page": r.get("page_title") or r.get("url"),
        "URL": r.get("url"),
        "Page Intent": f"{r.get('intent')} ({r.get('intent_confidence', 0)})",
        "Current Schema Types": ", ".join(r.get("schema_types", [])) or "(none)",
        "Current Schema Code": verbatim or "(none)",
        "Validity": r.get("validity"),
        "Coverage": r.get("coverage"),
        "schema.org errors": sorg_err,
        "schema.org warnings": sorg_warn,
        "Google eligibility": goog,
        "Recommendation": recs,
        "Generated JSON-LD": (r.get("generated") or {}).get("pretty", ""),
        "Priority": r.get("priority"),
    }


def build_xlsx(batch: dict) -> bytes:
    from openpyxl import Workbook
    from ..report.xlsx_export import _WRAP_COLS, _save, _write_sheet
    _WRAP_COLS.update({"Current Schema Code", "Generated JSON-LD", "schema.org errors",
                       "schema.org warnings", "Google eligibility", "Recommendation", "URL"})
    wb = Workbook()
    ws = wb.active
    ws.title = "Schema check"
    rows = [_row(r) for r in batch.get("results", [])]
    title = f"Schema Checker — {batch.get('count', len(rows))} pages · ruleset {batch.get('ruleset_version','')}"
    _write_sheet(ws, title, COLS, rows)
    if batch.get("cross_page_org"):
        ws2 = wb.create_sheet("Cross-page Org")
        _write_sheet(ws2, "Cross-page Organization consistency",
                     ["Issue"], [{"Issue": c} for c in batch["cross_page_org"]])
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def build_json(batch: dict) -> str:
    """Full machine-readable report (drops the heavy raw HTML, keeps everything analytical)."""
    slim = []
    for r in batch.get("results", []):
        d = {k: v for k, v in r.items() if k not in ("signals",)}
        slim.append(d)
    return json.dumps({"count": batch.get("count"), "ruleset_version": batch.get("ruleset_version"),
                       "cross_page_org": batch.get("cross_page_org", []), "results": slim},
                      indent=2, ensure_ascii=False, default=str)
