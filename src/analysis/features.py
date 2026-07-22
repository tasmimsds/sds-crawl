"""Issue 6: extract product-feature claims and compare against features.yaml."""
from __future__ import annotations

from ..db import record_issue
from ..rules import load_features
from ..util import context_around
from ._common import english_pages, features_context, gather_limited
from .llm import LlmClient

_SYS = (
    "You audit SDS Manager marketing/blog pages for inaccurate product-feature claims. "
    "You compare claims against the authoritative feature list (with real statuses). "
    "Flag claims about features that are 'partial' or 'not_available', and claims that "
    "materially misdescribe an 'available' feature. Respond ONLY with JSON."
)


def _user(features_c, body):
    return (
        f"AUTHORITATIVE FEATURE LIST (status is the truth):\n{features_c}\n\n"
        "Extract every product-feature claim in the PAGE TEXT and compare to the list. "
        "Flag only genuine problems. Return JSON {\"claims\":[{\"quote\":\"<exact text>\","
        "\"feature_id\":\"<matching id>\",\"problem\":\"not_available|partial|misdescription\","
        "\"explanation\":\"...\",\"severity\":\"high|medium\"}]}. Empty array if none."
    )


async def analyze_features_llm(conn, source_id: int, *, all_locales: bool = False):
    llm = LlmClient(conn)
    if not llm.enabled:
        print("LLM features analysis skipped: no OPENROUTER_API_KEY.")
        return 0
    features_c = features_context(conn)
    valid_ids = {f["id"] for f in load_features(conn)}
    pages = english_pages(conn, source_id, all_locales)
    stats = {"issues": 0}

    def make(page):
        async def run():
            body = page["body_text"][: llm.max_body]
            h = page["content_hash"] or page["url"]
            data = await llm.call_json(task="features", model=llm.reasoning_model,
                                       cache_key=f"{h}|feat", system=_SYS,
                                       user=_user(features_c, body))
            for c in (data or {}).get("claims", []):
                if not c.get("quote"):
                    continue
                problem = c.get("problem", "misdescription")
                severity = "high" if problem == "not_available" else (
                    "high" if c.get("severity") == "high" else "medium")
                fid = c.get("feature_id") if c.get("feature_id") in valid_ids else "?"
                quote = c["quote"]
                idx = page["body_text"].find(quote)
                if idx < 0:
                    continue  # guardrail: quote must be verbatim page text (not the feature list)
                record_issue(
                    conn, source_id=source_id, url_id=page["url_id"], category="feature_claim",
                    severity=severity, title=f"feature:{fid}:{problem}",
                    detail=c.get("explanation"), evidence=quote,
                    expected=f"feature '{fid}' status per features.yaml", detection_method="llm",
                )
                stats["issues"] += 1
        return run

    await gather_limited([make(p) for p in pages], limit=6)
    conn.commit()
    llm.log_usage()
    print(f"LLM features analysis: {stats['issues']} issues across {len(pages)} pages.")
    return stats["issues"]
