"""Two-layer brand scoping: keep only passages that are specifically about THIS brand.

Layer 1 (cheap): passage must mention the brand name or an alias.
Layer 2 (LLM): 'is this passage specifically about {brand} (at {own_domains}), or a
competitor / generic topic?' -> about_brand + confidence. Only about_brand passages
proceed to fact checking; discarded ones are logged (never shown as findings).
"""
from __future__ import annotations

import re

from ..analysis.llm import LlmClient
from ..db import now_iso
from ..util import normalize_text, sha256
from .brand import ensure_brand_profile

_SENT = re.compile(r"(?<=[.!?])\s+")


def _candidates(text: str, terms: list[str]) -> list[str]:
    """Sentences mentioning the brand, each with ±1 sentence of context."""
    sents = _SENT.split(text)
    rx = re.compile("|".join(re.escape(t) for t in terms if t), re.I)
    seen, out = set(), []
    for i, s in enumerate(sents):
        if not rx.search(s):
            continue
        passage = normalize_text(" ".join(sents[max(0, i - 1): i + 2]))[:600]
        key = passage.lower()
        if passage and key not in seen:
            seen.add(key)
            out.append(passage)
    return out


def _sys(brand):
    return (
        f"You decide whether a passage from a third-party web page is SPECIFICALLY about the "
        f"product '{brand['brand_name']}' (website: {', '.join(brand['own_domains'][:2])}), as "
        f"opposed to a competitor, a different product with a similar name, or a generic topic. "
        f"{brand.get('disambiguation_notes') or ''} "
        + (f"Competitor names to watch: {', '.join(brand['negative_terms'])}. "
           if brand.get("negative_terms") else "")
        + "Respond ONLY with JSON."
    )


async def scope_pages(conn, source_id: int, on_progress=None) -> dict:
    brand = ensure_brand_profile(conn, source_id)
    terms = [brand["brand_name"], *brand.get("aliases", [])]
    llm = LlmClient(conn)
    pages = conn.execute(
        """SELECT id, text, source_type, context_paragraph FROM external_pages
           WHERE source_id=? AND fetch_status='ok'
             AND (text IS NOT NULL OR context_paragraph IS NOT NULL)""",
        (source_id,)).fetchall()

    kept = discarded = 0
    for pi, page in enumerate(pages):
        conn.execute("DELETE FROM external_snippets WHERE page_id=?", (page["id"],))
        # backlink/mention items scope on their FULL paragraph (kept intact, not sentence-split);
        # manual pages fall back to brand-sentence extraction.
        if page["source_type"] in ("backlink", "mention") and page["context_paragraph"]:
            cands = [page["context_paragraph"]]
        else:
            cands = _candidates(page["text"] or "", terms)
        # LLM layer (batched)
        verdicts = {}
        if llm.enabled and cands:
            for start in range(0, len(cands), 8):
                chunk = cands[start:start + 8]
                import json as _json
                payload = [{"i": start + k, "text": c} for k, c in enumerate(chunk)]
                data = await llm.call_json(
                    task="brand_scope", model=llm.fast_model,
                    cache_key=sha256(brand["brand_name"] + _json.dumps(payload)) + "|scope",
                    system=_sys(brand),
                    user=("Passages (JSON):\n" + _json.dumps(payload, ensure_ascii=False) +
                          '\n\nReturn JSON {"items":[{"i":<index>,"about_brand":true|false,'
                          '"confidence":0..1,"reason":"short"}]}.'),
                )
                for v in (data or {}).get("items", []):
                    verdicts[v["i"]] = v   # keyed by global candidate index
        # store every candidate with its scoping decision (discarded ones logged too)
        for k, c in enumerate(cands):
            v = verdicts.get(k)
            about = bool(v and v.get("about_brand")) if llm.enabled else True
            conf = float(v.get("confidence", 0)) if v else (0.5 if not llm.enabled else 0.0)
            reason = (v.get("reason", "") if v else
                      ("LLM unavailable — kept" if not llm.enabled else "no verdict"))
            conn.execute(
                """INSERT INTO external_snippets (page_id, source_id, snippet, about_brand,
                     confidence, reason, position, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (page["id"], source_id, c, 1 if about else 0, conf, reason, k, now_iso()),
            )
            kept += about
            discarded += (not about)
        conn.commit()
        if on_progress:
            on_progress(pi + 1, len(pages), 0)
    return {"pages": len(pages), "brand_snippets": kept, "discarded": discarded}
