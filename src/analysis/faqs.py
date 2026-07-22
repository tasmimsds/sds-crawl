"""Issue 7: FAQ mismatch detection. FAQs are high-visibility (rich results),
so findings get their own `faq` category, severity >= medium."""
from __future__ import annotations

from ..config import settings
from ..db import record_issue
from ..util import sha256
from ._common import facts_context, features_context, gather_limited
from .llm import LlmClient

_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SYS = (
    "You audit FAQ answers on SDS Manager pages. FAQs power Google/AI rich answers, so "
    "accuracy is critical. Flag any answer that contradicts the source-of-truth facts or "
    "feature list: wrong DB size (truth 17M+), claims of a FREE PLAN (free trial is fine), "
    "small-business positioning, wrong language/region counts, or inaccurate feature claims. "
    "Respond ONLY with JSON."
)


def _min_medium(sev: str) -> str:
    return sev if _SEV_RANK.get(sev, 3) <= _SEV_RANK["medium"] else "medium"


def _grouped_faqs(conn, source_id: int, all_locales: bool):
    english = set(settings()["llm"]["english_locales"])
    rows = conn.execute(
        """SELECT f.id, f.url_id, u.url, u.locale, f.question, f.answer, f.source
           FROM faqs f JOIN urls u ON u.id=f.url_id
           WHERE u.source_id=?""",
        (source_id,),
    ).fetchall()
    groups: dict[int, dict] = {}
    for r in rows:
        if not all_locales and not (r["locale"] is None or r["locale"] in english):
            continue
        if "sds manager" not in (r["answer"] or "").lower() and \
           "sds manager" not in (r["question"] or "").lower():
            continue
        g = groups.setdefault(r["url_id"], {"url": r["url"], "faqs": []})
        g["faqs"].append({"question": r["question"], "answer": r["answer"]})
    return groups


async def analyze_faqs(conn, source_id: int, *, all_locales: bool = False):
    llm = LlmClient(conn)
    if not llm.enabled:
        print("LLM FAQ analysis skipped: no OPENROUTER_API_KEY.")
        return 0
    facts_c, features_c = facts_context(conn), features_context(conn)
    groups = _grouped_faqs(conn, source_id, all_locales)
    stats = {"issues": 0}

    import json

    def make(url_id, g):
        async def run():
            block = json.dumps(g["faqs"], ensure_ascii=False)
            key = sha256(block) + "|faq"
            user = (
                f"FACTS:\n{facts_c}\n\nFEATURES:\n{features_c}\n\nFAQs (JSON):\n{block}\n\n"
                'Flag problematic answers. Return JSON {"items":[{"question":"...",'
                '"quote":"<exact wrong text from the answer>","underlying_category":'
                '"database_size|free_claim|positioning|language_count|region_count|feature_claim|other_mismatch",'
                '"severity":"critical|high|medium|low","explanation":"...","expected":"<correct value or null>"}]}. '
                "Empty array if all answers are fine."
            )
            data = await llm.call_json(task="faq", model=llm.reasoning_model,
                                       cache_key=key, system=_SYS, user=user)
            answers_text = " ".join(f["answer"] or "" for f in g["faqs"])
            for item in (data or {}).get("items", []):
                if not item.get("quote"):
                    continue
                if item["quote"] not in answers_text:
                    continue  # guardrail: quote must be verbatim from a FAQ answer
                record_issue(
                    conn, source_id=source_id, url_id=url_id, category="faq",
                    severity=_min_medium(item.get("severity", "medium")),
                    title=f"faq:{item.get('underlying_category','other_mismatch')}:"
                          f"{(item.get('question') or item['quote'])[:32]}",
                    detail=f"[{item.get('underlying_category')}] {item.get('explanation','')}",
                    evidence=item["quote"], expected=item.get("expected"),
                    detection_method="llm",
                )
                stats["issues"] += 1
        return run

    await gather_limited([make(uid, g) for uid, g in groups.items()], limit=6)
    conn.commit()
    llm.log_usage()
    print(f"LLM FAQ analysis: {stats['issues']} issues across {len(groups)} FAQ pages.")
    return stats["issues"]
