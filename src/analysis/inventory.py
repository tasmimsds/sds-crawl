"""Site-wide claim inventory + consistency check (Issues 4 & 5).

Free, no LLM. Inventories every language/region/translation/DB-size/numeric
claim, groups by value, and (when facts.yaml sets a canonical value) flags
pages that disagree.
"""
from __future__ import annotations

import csv
import re

from ..config import resolve_path, settings
from ..rules import load_rules
from ..db import record_issue, record_match, rule_pk
from ..util import context_around, normalize_text

# generic numeric claims for the inventory CSV
_GENERIC = {
    "million/M": re.compile(r"\b\d[\d.,]*\+?\s*(?:million|billion|M|B)\b", re.I),
    "count": re.compile(r"\b\d[\d.,]*\+?\s*(?:companies|customers|users|organi[sz]ations|clients)\b", re.I),
    "percent": re.compile(r"\b\d[\d.,]*\s*%"),
    "price": re.compile(r"[$€£]\s?\d[\d.,]*"),
}


def _claim_quote(body: str, m) -> str:
    """Evidence anchored on the matched claim: the match itself + a little
    trailing context, so the number+unit leads the quote (not buried)."""
    return normalize_text(body[m.start(): m.end() + 45])


def _latest_bodies(conn, source_id):
    return conn.execute(
        """SELECT u.id AS url_id, u.url, u.locale, c.body_text
           FROM urls u
           JOIN crawl_results c ON c.id = (
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1
           )
           WHERE u.source_id=? AND c.body_text IS NOT NULL AND c.body_text != ''""",
        (source_id,),
    ).fetchall()


def collect_inventory(conn, source_id):
    """Return {kind: {'label','category','canonical','values':{val:[(url_id,url,quote)]}}}."""
    rows = _latest_bodies(conn, source_id)
    inv: dict = {}

    def ensure(kind, label, category, canonical, product_id=None):
        inv.setdefault(kind, {"label": label, "category": category, "canonical": canonical,
                              "product_id": product_id, "values": {}})

    # fact-driven claim inventories (languages, regions, translations, db size)
    fact_patterns = []
    for f in load_rules(conn):
        pats = f.get("claim_patterns")
        if not pats:
            continue
        ensure(f["id"], f.get("description", f["id"]), f.get("category"),
               f.get("canonical_value"), f.get("product_id"))
        fact_patterns.append((f["id"], [re.compile(p, re.I) for p in pats]))

    for kind in _GENERIC:
        ensure(kind, kind, None, None)

    for page in rows:
        body = page["body_text"]
        for fid, regexes in fact_patterns:
            for rx in regexes:
                for m in rx.finditer(body):
                    val = re.sub(r"[.,]$", "", m.group(1).strip())
                    # Evidence = the exact matched claim (e.g. "29 languages"), with a
                    # little surrounding context appended so it's locatable.
                    quote = _claim_quote(body, m)
                    inv[fid]["values"].setdefault(val, []).append((page["url_id"], page["url"], quote))
        for kind, rx in _GENERIC.items():
            for m in rx.finditer(body):
                val = re.sub(r"\s+", " ", m.group(0).strip().lower())
                quote = _claim_quote(body, m)
                inv[kind]["values"].setdefault(val, []).append((page["url_id"], page["url"], quote))
    return inv


def consistency_check(conn, source_id: int) -> int:
    inv = collect_inventory(conn, source_id)
    issues = 0

    for kind, data in inv.items():
        values = data["values"]
        if not values or data["category"] is None:
            continue  # generic buckets are inventory-only
        distinct = sorted(values.items(), key=lambda kv: -len(kv[1]))
        summary = ", ".join(f"{v}×{len(occ)}" for v, occ in distinct)
        print(f"  inventory[{kind}]: {summary or '(none)'}")

        canonical = data["canonical"]
        if canonical in (None, "", "null"):
            continue  # not decided yet -> inventory only, no flags
        canonical = str(canonical)
        pk = rule_pk(conn, kind)
        prod = data.get("product_id")
        for val, occ in values.items():
            match = str(val) == canonical
            seen = set()
            for url_id, _url, quote in occ:
                if url_id in seen:
                    continue
                seen.add(url_id)
                if match:
                    record_match(conn, fact_rule_id=pk, url_id=url_id, verdict="positive",
                                 evidence=quote, matched_value=str(val), product_id=prod)
                    continue
                record_issue(
                    conn, source_id=source_id, url_id=url_id,
                    category=data["category"], severity="high",
                    title=f"{kind}:inconsistent",
                    detail=f"{data['label']}: page claims '{val}' but canonical is '{canonical}'.",
                    evidence=quote, expected=canonical, detection_method="inventory",
                    product_id=prod,
                )
                record_match(conn, fact_rule_id=pk, url_id=url_id, verdict="issue",
                             evidence=quote, matched_value=str(val), product_id=prod)
                issues += 1
    conn.commit()
    print(f"Consistency check: {issues} mismatch issues (canonical values only).")
    return issues


def export_claims(conn, source_id: int):
    inv = collect_inventory(conn, source_id)
    out_path = resolve_path(f"{settings()['paths']['output_dir']}/detected_claims.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:  # BOM for Excel
        w = csv.writer(fh)
        w.writerow(["kind", "value", "frequency", "example_url", "example_quote"])
        for kind, data in inv.items():
            for val, occ in sorted(data["values"].items(), key=lambda kv: -len(kv[1])):
                ex_url = occ[0][1]
                ex_quote = occ[0][2]
                w.writerow([kind, val, len(occ), ex_url, ex_quote])
                n += 1
    print(f"Claim inventory: {n} distinct claims -> {out_path}")
    return out_path
