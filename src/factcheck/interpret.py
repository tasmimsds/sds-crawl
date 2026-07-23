"""Turn free-text fact input into structured, checkable fact(s) via the LLM.

Handles: short "core point" or long detailed text, multi-fact splitting, and a
vague-input fallback (no correct_value -> plain search mode).
"""
from __future__ import annotations

from ..analysis.llm import LlmClient
from ..util import sha256

_CATS = ["database_size", "positioning", "free_claim", "language_count",
         "region_count", "regulation_count", "feature_claim", "other_mismatch"]

_SYS = (
    "You convert a user's plain-language statement of a FACT about their own website "
    "into a structured, checkable fact. The goal is to HUNT FOR MISTAKES on the site, so "
    "search terms must include the WRONG/old values and phrasings too — not just the correct "
    "one. Include numeric format variants (17M / 17 million / 17,000,000) and close paraphrases. "
    "If a fact has an allowed exception (e.g. 'free trial' is fine while 'free plan' is wrong), "
    "put the allowed wording in allowed_mentions. If the text contains MORE THAN ONE distinct "
    "fact, return each separately. If the text is too vague to extract a correct value, still "
    "return search terms and set correct_value to null. Respond ONLY with JSON."
)


def _user(text: str) -> str:
    return (
        f"Categories to choose from: {', '.join(_CATS)} (use other_mismatch if none fit).\n\n"
        f'USER TEXT:\n"""{text}"""\n\n'
        'Return JSON: {"facts":[{'
        '"fact_name":"short label","claim_topic":"what this is about",'
        '"correct_value":"the right value, or null if unclear",'
        '"category":"one of the categories",'
        '"search_terms":["...correct AND wrong values/phrasings, numeric variants..."],'
        '"stale_indicators":["...the wrong/outdated values to flag..."],'
        '"allowed_mentions":["...wording that is CORRECT and must NOT be flagged..."],'
        '"notes":"..."}]}. '
        "Split multiple distinct facts into separate array items."
    )


def _norm_list(v):
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def _assign_product(conn, project_id, text, fact):
    """Deterministically tag a fact with a product from the project's product list.
    A product NAME in the text NEVER creates a project — projects come only from the
    Add Website flow. Falls back to the project's default product."""
    from ..db import default_product_id, match_product
    if not project_id:
        return
    hay = " ".join(str(fact.get(k) or "") for k in ("fact_name", "claim_topic", "notes")) + " " + text
    pid = match_product(conn, project_id, hay)
    if pid is None:
        pid = default_product_id(conn, project_id)
    fact["product_id"] = pid
    row = conn.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone() if pid else None
    fact["product_name"] = row["name"] if row else None


async def interpret(conn, text: str, project_id: int | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        return {"facts": [], "vague": True}
    llm = LlmClient(conn)
    if not llm.enabled:
        # no LLM -> treat whole text as a plain search
        f = {"fact_name": text[:60], "claim_topic": text[:120],
             "correct_value": None, "category": "other_mismatch",
             "search_terms": [text], "stale_indicators": [],
             "allowed_mentions": [], "notes": "LLM unavailable — plain search."}
        _assign_product(conn, project_id, text, f)
        return {"facts": [f], "vague": True}

    data = await llm.call_json(task="interpret", model=llm.interpret_model,
                               cache_key=sha256(text) + "|interp", system=_SYS, user=_user(text))
    # the model may return {"facts":[...]}, a bare [...], or a single {...}
    if isinstance(data, list):
        raw_facts = data
    elif isinstance(data, dict):
        raw_facts = data.get("facts") if isinstance(data.get("facts"), list) else [data]
    else:
        raw_facts = []
    facts = []
    for f in raw_facts:
        if not isinstance(f, dict):
            continue
        cat = f.get("category") if f.get("category") in _CATS else "other_mismatch"
        cv = f.get("correct_value")
        cv = None if cv in (None, "", "null", "unclear") else str(cv)
        terms = _norm_list(f.get("search_terms")) or [text]
        facts.append({
            "fact_name": (f.get("fact_name") or text[:60]).strip(),
            "claim_topic": (f.get("claim_topic") or "").strip(),
            "correct_value": cv,
            "category": cat,
            "search_terms": terms,
            "stale_indicators": _norm_list(f.get("stale_indicators")),
            "allowed_mentions": _norm_list(f.get("allowed_mentions")),
            "notes": (f.get("notes") or "").strip(),
        })
    if not facts:  # fallback to plain search on the raw text
        facts = [{"fact_name": text[:60], "claim_topic": text[:120], "correct_value": None,
                  "category": "other_mismatch", "search_terms": [text], "stale_indicators": [],
                  "allowed_mentions": [], "notes": "Could not extract a value — plain search."}]
    for f in facts:
        _assign_product(conn, project_id, text, f)
    vague = all(f["correct_value"] is None for f in facts)
    return {"facts": facts, "vague": vague}
