"""Two-stage LLM fact/positioning/free/count analysis + other_mismatch (Issues 1-5, +)."""
from __future__ import annotations

import re

from ..db import CATEGORIES, record_issue
from ..util import context_around

_FREE_TRIAL_CTX = re.compile(r"free trial|\btrial\b", re.I)
_FREE_PLAN_HARD = re.compile(
    r"free (?:plan|version|option|tier|forever|account|subscription|edition)|forever free"
    r"|\bfor free\b|free of charge|free to use|completely free|100% free",
    re.I,
)
from ._common import english_pages, facts_context, features_context, gather_limited
from .llm import LlmClient

_ALLOWED_CATS = {
    "database_size", "positioning", "free_claim", "language_count",
    "region_count", "feature_claim", "other_mismatch",
}

_SCREEN_SYS = (
    "You are a meticulous QA analyst for SDS Manager. You detect statements on the "
    "company's own marketing/blog pages that are outdated, inconsistent, or contradict "
    "the source-of-truth registry. Respond ONLY with JSON."
)
_VERIFY_SYS = (
    "You verify flagged claims for SDS Manager and DISCARD false positives. Be strict: "
    "confirm an issue only when the exact quote genuinely says something wrong vs the "
    "registry. 'Free trial' is CORRECT and must never be flagged as a free-plan issue. "
    "Respond ONLY with JSON."
)


def _screen_user(facts_c, features_c, body):
    return (
        f"SOURCE-OF-TRUTH FACTS:\n{facts_c}\n\nFEATURES:\n{features_c}\n\n"
        "From the PAGE TEXT, list every claim about SDS Manager that (a) contradicts or "
        "gives an outdated version of a fact, (b) positions the product for small "
        "businesses/SMBs, (c) claims a FREE PLAN/version/tier (NOT a free trial), (d) states "
        "a language/region/country/translation count, or (e) is any other internal "
        "inconsistency (contradictory numbers, outdated years presented as current, "
        "self-contradiction).\n"
        'Return JSON {"claims":[{"claim":"...","quote":"<exact text from page>",'
        '"category":"database_size|positioning|free_claim|language_count|region_count|other_mismatch",'
        '"confidence":0..1}]}. Empty array if none.\n\nPAGE TEXT:\n' + body
    )


def _verify_user(facts_c, claims):
    import json
    return (
        f"SOURCE-OF-TRUTH FACTS:\n{facts_c}\n\nCandidate claims from screening:\n"
        + json.dumps(claims, ensure_ascii=False, indent=2)
        + '\n\nFor each, decide if it is a REAL issue. Return JSON {"items":[{"quote":'
        '"<exact quote>","is_real_issue":true|false,"category":"<one of the categories>",'
        '"severity":"critical|high|medium|low","explanation":"...","expected":"<correct value or null>"}]}.'
    )


async def fact_check_llm(conn, source_id: int, *, all_locales: bool = False):
    llm = LlmClient(conn)
    if not llm.enabled:
        print("LLM fact-check skipped: no OPENROUTER_API_KEY (regex + inventory already ran).")
        return 0
    facts_c, features_c = facts_context(conn), features_context(conn)
    pages = english_pages(conn, source_id, all_locales)
    max_body = llm.max_body

    stats = {"flagged": 0, "issues": 0}

    def make(page):
        async def run():
            body = page["body_text"][:max_body]
            h = page["content_hash"] or page["url"]
            s1 = await llm.call_json(task="fact_check", model=llm.fast_model,
                                     cache_key=f"{h}|fc_s1", system=_SCREEN_SYS,
                                     user=_screen_user(facts_c, features_c, body))
            claims = [c for c in (s1 or {}).get("claims", [])
                      if c.get("quote") and c.get("confidence", 0) >= 0.4]
            if not claims:
                return
            stats["flagged"] += 1
            s2 = await llm.call_json(task="fact_check", model=llm.reasoning_model,
                                     cache_key=f"{h}|fc_s2", system=_VERIFY_SYS,
                                     user=_verify_user(facts_c, claims))
            for item in (s2 or {}).get("items", []):
                if not item.get("is_real_issue") or not item.get("quote"):
                    continue
                cat = item.get("category") if item.get("category") in _ALLOWED_CATS else "other_mismatch"
                if cat not in CATEGORIES:
                    cat = "other_mismatch"
                quote = item["quote"]
                idx = page["body_text"].find(quote)
                if idx < 0:
                    continue  # guardrail: evidence quote must exist verbatim on the page
                # 'free trial' is correct — drop free_claim hits that are trial-context
                # only, with no actual free-plan phrasing.
                if cat == "free_claim" and _FREE_TRIAL_CTX.search(quote) and not _FREE_PLAN_HARD.search(quote):
                    continue
                record_issue(
                    conn, source_id=source_id, url_id=page["url_id"], category=cat,
                    severity=item.get("severity", "medium"),
                    title=f"llm:{cat}:{(item.get('expected') or quote)[:32]}",
                    detail=item.get("explanation"), evidence=quote,
                    expected=item.get("expected"), detection_method="llm",
                )
                stats["issues"] += 1
        return run

    await gather_limited([make(p) for p in pages], limit=6)
    conn.commit()
    llm.log_usage()
    print(f"LLM fact-check: {stats['flagged']} pages flagged, {stats['issues']} confirmed issues "
          f"across {len(pages)} pages.")
    return stats["issues"]
