"""Context-aware, product-specific claim analysis for units that DIFFER by product.

SDS Manager and ExactSDS both talk about "N languages" and "N regulations" but with
different correct numbers, and SDS Manager languages are themselves context-specific
(32 in the inventory/platform vs 67 for SDS documents). A plain exact-match inventory
would false-positive across these, so we classify each claim by the context around it:

  languages:  ExactSDS ctx -> 32 | SDS-document ctx -> 67 | inventory/platform ctx -> 32
              | no qualifying context -> UNCLEAR (team must clarify which meaning)
  regulations: ExactSDS ctx -> 8 | otherwise SDS Manager -> 49

Values are read from the fact rules (by slug) so editing a rule updates detection.
"""
from __future__ import annotations

import re

from ..db import default_product_id, record_issue, record_match
from ..util import context_around

_WIN = 160  # chars of context each side of a claim

_EXACTSDS_CTX = re.compile(r"exact\s*sds", re.I)
_SDS_CTX = re.compile(r"\bSDS\b|\bSDSs\b|safety[\s-]data[\s-]sheet|\bdata sheet|\bdocument|\blibrary\b", re.I)
_INV_CTX = re.compile(r"\binventory\b|\bplatform\b|\binterface\b|\bdashboard\b|\bportal\b"
                      r"|\bsystem\b|user interface|\bthe app\b|\bUI\b", re.I)

_LANG_RE = re.compile(r"(\d{1,3})\s*(?:\+\s*)?(?:different\s+)?languages\b", re.I)
_REG_RE = re.compile(r"(\d{1,3})\s*(?:\+\s*)?regulations\b", re.I)
# ExactSDS claims that regulations are fixed/hardcoded (they are extensible via builder)
_FIXED_REG_RE = re.compile(r"(?:fixed|hard[\s-]?coded|pre[\s-]?defined|cannot (?:add|be added)|"
                           r"no (?:new )?regulations? can be added)\b[^.]{0,40}regulation", re.I)


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


def _rule_values(conn):
    """slug -> (correct_value, product_id, rule_pk) for the product-specific metric rules."""
    out = {}
    for r in conn.execute(
        "SELECT id, slug, correct_value, product_id FROM fact_rules WHERE slug IN "
        "('sdsmanager_inventory_languages','sdsmanager_sds_languages','exactsds_languages',"
        " 'sdsmanager_regulations','exactsds_regulations')"):
        out[r["slug"]] = (r["correct_value"], r["product_id"], r["id"])
    return out


def _num(correct: str | None) -> str | None:
    m = re.search(r"\d{1,3}", correct or "")
    return m.group(0) if m else None


def analyze_product_claims(conn, source_id: int) -> dict:
    """Flag product-specific language/regulation claims; surface ambiguous ones as
    'unclear/needs context'. Returns per-slug counts for reporting."""
    vals = _rule_values(conn)
    _NA = (None, None, None)
    inv_lang = _num(vals.get("sdsmanager_inventory_languages", _NA)[0]) or "32"
    sds_lang = _num(vals.get("sdsmanager_sds_languages", _NA)[0]) or "67"
    ex_lang = _num(vals.get("exactsds_languages", _NA)[0]) or "32"
    sdsm_reg = _num(vals.get("sdsmanager_regulations", _NA)[0]) or "49"
    ex_reg = _num(vals.get("exactsds_regulations", _NA)[0]) or "8"

    pid = {
        "inv": vals.get("sdsmanager_inventory_languages", _NA)[1],
        "sds": vals.get("sdsmanager_sds_languages", _NA)[1],
        "ex_lang": vals.get("exactsds_languages", _NA)[1],
        "sdsm_reg": vals.get("sdsmanager_regulations", _NA)[1],
        "ex_reg": vals.get("exactsds_regulations", _NA)[1],
    }
    rpk = {
        "inv": vals.get("sdsmanager_inventory_languages", _NA)[2],
        "sds": vals.get("sdsmanager_sds_languages", _NA)[2],
        "ex_lang": vals.get("exactsds_languages", _NA)[2],
        "sdsm_reg": vals.get("sdsmanager_regulations", _NA)[2],
        "ex_reg": vals.get("exactsds_regulations", _NA)[2],
    }
    default_pid = default_product_id(conn, source_id)
    stats = {"lang_open": 0, "lang_positive": 0, "lang_unclear": 0,
             "reg_open": 0, "reg_positive": 0, "reg_fixed": 0}

    for page in _latest_bodies(conn, source_id):
        body = page["body_text"]
        # ---- language claims ----
        for m in _LANG_RE.finditer(body):
            n = m.group(1)
            seg = body[max(0, m.start() - _WIN): m.end() + _WIN]
            quote = context_around(body, m.start(), m.end() - m.start())
            has_ex = bool(_EXACTSDS_CTX.search(seg))
            has_sds = bool(_SDS_CTX.search(seg))
            has_inv = bool(_INV_CTX.search(seg))
            if has_ex:
                expected, who, method, pk = ex_lang, pid["ex_lang"], "ExactSDS", rpk["ex_lang"]
            elif has_sds and not has_inv:
                expected, who, method, pk = sds_lang, pid["sds"], "SDS documents", rpk["sds"]
            elif has_inv and not has_sds:
                expected, who, method, pk = inv_lang, pid["inv"], "inventory/platform", rpk["inv"]
            else:
                # no qualifying context, OR both inventory AND SDS context (genuinely
                # ambiguous) -> the team must decide which meaning is intended.
                record_issue(
                    conn, source_id=source_id, url_id=page["url_id"],
                    category="language_count", severity="medium",
                    title=f"lang_unclear:{n}",
                    detail=(f"Ambiguous language count: page says '{n} languages' without a "
                            f"clear single context. Inventory/platform is {inv_lang}, SDS "
                            f"documents are {sds_lang} — reword to specify which."),
                    evidence=quote, expected=f"{inv_lang} (inventory) or {sds_lang} (SDS documents)",
                    detection_method="context", status="unclear", product_id=default_pid,
                )
                record_match(conn, fact_rule_id=rpk["sds"], url_id=page["url_id"],
                             verdict="unclear", evidence=quote, matched_value=n, product_id=pid["sds"])
                stats["lang_unclear"] += 1
                continue
            if n != expected:
                record_issue(
                    conn, source_id=source_id, url_id=page["url_id"],
                    category="language_count", severity="high",
                    title=f"lang_ctx:{method}:{n}",
                    detail=f"{method} languages should be {expected}, page says '{n} languages'.",
                    evidence=quote, expected=expected, detection_method="context", product_id=who,
                )
                record_match(conn, fact_rule_id=pk, url_id=page["url_id"], verdict="issue",
                             evidence=quote, matched_value=n, product_id=who)
                stats["lang_open"] += 1
            else:
                record_match(conn, fact_rule_id=pk, url_id=page["url_id"], verdict="positive",
                             evidence=quote, matched_value=n, product_id=who)
                stats["lang_positive"] += 1

        # ---- regulation claims ----
        for m in _REG_RE.finditer(body):
            n = m.group(1)
            seg = body[max(0, m.start() - _WIN): m.end() + _WIN]
            quote = context_around(body, m.start(), m.end() - m.start())
            if _EXACTSDS_CTX.search(seg):
                expected, who, method, pk = ex_reg, pid["ex_reg"], "ExactSDS", rpk["ex_reg"]
            else:
                expected, who, method, pk = sdsm_reg, pid["sdsm_reg"], "SDS Manager", rpk["sdsm_reg"]
            if n != expected:
                record_issue(
                    conn, source_id=source_id, url_id=page["url_id"],
                    category="regulation_count", severity="high",
                    title=f"reg_ctx:{method}:{n}",
                    detail=f"{method} supports {expected} regulations, page says '{n} regulations'.",
                    evidence=quote, expected=expected, detection_method="context", product_id=who,
                )
                record_match(conn, fact_rule_id=pk, url_id=page["url_id"], verdict="issue",
                             evidence=quote, matched_value=n, product_id=who)
                stats["reg_open"] += 1
            else:
                record_match(conn, fact_rule_id=pk, url_id=page["url_id"], verdict="positive",
                             evidence=quote, matched_value=n, product_id=who)
                stats["reg_positive"] += 1

        # ---- ExactSDS 'regulations are fixed' claims ----
        for m in _FIXED_REG_RE.finditer(body):
            seg = body[max(0, m.start() - _WIN): m.end() + _WIN]
            if not _EXACTSDS_CTX.search(seg):
                continue
            record_issue(
                conn, source_id=source_id, url_id=page["url_id"],
                category="regulation_count", severity="high",
                title="exactsds_reg_fixed",
                detail="ExactSDS regulations are NOT fixed — the admin Regulation Builder adds "
                       "new regulations with no code change. This claim is wrong.",
                evidence=context_around(body, m.start(), m.end() - m.start()),
                detection_method="context", product_id=pid["ex_reg"],
            )
            stats["reg_fixed"] += 1

    conn.commit()
    print(f"Product claims: languages {stats['lang_positive']}✓ / {stats['lang_open']}✗ / "
          f"{stats['lang_unclear']}? · regulations {stats['reg_positive']}✓ / "
          f"{stats['reg_open']}✗ · {stats['reg_fixed']} fixed-regulation claims.")
    return stats
