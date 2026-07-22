"""Phase 5: within-locale keyword cannibalization (never across hreflang variants)."""
from __future__ import annotations

import json
import math
import re

from ..config import settings
from ..db import record_issue
from .llm import LlmClient

_TOKEN = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if len(t) >= 2]


def _tfidf(docs: list[list[str]]) -> list[dict]:
    n = len(docs)
    df: dict[str, int] = {}
    for toks in docs:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log((n + 1) / (d + 1)) + 1 for t, d in df.items()}
    vecs = []
    for toks in docs:
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        vec = {t: c * idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vecs.append({t: v / norm for t, v in vec.items()})
    return vecs


def _cosine(a: dict, b: dict) -> float:
    small, large = (a, b) if len(a) < len(b) else (b, a)
    return sum(w * large.get(t, 0.0) for t, w in small.items())


def _norm_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


async def analyze_cannibalization(conn, source_id: int):
    cfg = settings()["cannibalization"]
    threshold = cfg["similarity_threshold"]
    max_pairs = cfg["max_pairs_per_locale"]
    llm = LlmClient(conn)

    rows = conn.execute(
        """SELECT u.id AS url_id, u.url, u.locale, u.hreflang_group_id,
                  c.content_hash, c.title, c.meta_description, c.h1, c.body_text
           FROM urls u
           JOIN crawl_results c ON c.id = (
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1
           )
           WHERE u.source_id=? AND c.status_code>=200 AND c.status_code<300
             AND c.body_text IS NOT NULL AND c.body_text != ''""",
        (source_id,),
    ).fetchall()

    by_locale: dict[str, list] = {}
    for r in rows:
        by_locale.setdefault(r["locale"] or "", []).append(r)

    candidate_pairs = 0
    issues = 0

    for locale, group in by_locale.items():
        if len(group) < 2:
            continue
        pages = []
        for r in group:
            h1s = json.loads(r["h1"]) if r["h1"] else []
            h1 = h1s[0] if h1s else ""
            intro = (r["body_text"] or "")[:1000]
            pages.append({
                "row": r, "title": r["title"] or "", "h1": h1,
                "meta": r["meta_description"] or "",
                "doc": f"{r['title'] or ''} {h1} {r['meta_description'] or ''} {intro}",
                "intro": (r["body_text"] or "")[:500],
            })
        vecs = _tfidf([_tokenize(p["doc"]) for p in pages])

        cands = []
        for i in range(len(pages)):
            for j in range(i + 1, len(pages)):
                a, b = pages[i], pages[j]
                ga, gb = a["row"]["hreflang_group_id"], b["row"]["hreflang_group_id"]
                if ga is not None and ga == gb:
                    continue  # translations of each other
                sim = _cosine(vecs[i], vecs[j])
                dup_title = a["title"] and _norm_key(a["title"]) == _norm_key(b["title"])
                dup_h1 = a["h1"] and _norm_key(a["h1"]) == _norm_key(b["h1"])
                if sim >= threshold or dup_title or dup_h1:
                    cands.append((sim, dup_title, dup_h1, a, b))
        cands.sort(key=lambda c: -c[0])
        capped = cands[:max_pairs]
        candidate_pairs += len(capped)
        if len(capped) < len(cands):
            print(f"  ! locale '{locale}': {len(cands)} candidate pairs, capped to {max_pairs}.")

        for sim, dup_title, dup_h1, a, b in capped:
            if llm.enabled:
                key = "|".join(sorted([a["row"]["content_hash"] or a["row"]["url"],
                                       b["row"]["content_hash"] or b["row"]["url"]]))
                verdict = await llm.call_json(
                    task="cannibalization", model=llm.reasoning_model, cache_key=key,
                    system="You are an SEO strategist judging whether two same-locale pages "
                           "compete for the same search intent. Respond ONLY with JSON.",
                    user=(f"Locale '{locale}'.\n\nPAGE A\nURL: {a['row']['url']}\nTitle: {a['title']}\n"
                          f"H1: {a['h1']}\nMeta: {a['meta']}\nIntro: {a['intro']}\n\n"
                          f"PAGE B\nURL: {b['row']['url']}\nTitle: {b['title']}\nH1: {b['h1']}\n"
                          f"Meta: {b['meta']}\nIntro: {b['intro']}\n\n"
                          'Return JSON {"verdict":"cannibalizing|distinct|duplicate_content",'
                          '"primary_keyword":"...","recommendation":"merge|differentiate|canonicalize|deindex one + reason"}.'),
                )
                if not verdict or verdict.get("verdict") == "distinct":
                    continue
                issues += _write_pair(conn, source_id, sim, dup_title, dup_h1, a, b, verdict)
            else:
                if not (dup_title or dup_h1):
                    continue
                issues += _write_pair(conn, source_id, sim, dup_title, dup_h1, a, b, {
                    "verdict": "duplicate_content",
                    "primary_keyword": a["title"] or a["h1"],
                    "recommendation": "LLM unavailable — exact duplicate title/H1 in locale.",
                })

    conn.commit()
    if llm.enabled:
        llm.log_usage()
    print(f"Cannibalization: {candidate_pairs} candidate pairs, {issues} issues"
          + ("" if llm.enabled else " (offline: exact-duplicate only)") + ".")
    return issues


def _write_pair(conn, source_id, sim, dup_title, dup_h1, a, b, v) -> int:
    sev = "high" if v.get("verdict") == "duplicate_content" else "medium"
    detail = (f"Verdict: {v.get('verdict')}. Primary keyword: '{v.get('primary_keyword')}'. "
              f"Similarity {sim:.2f}"
              + (", duplicate title" if dup_title else "") + (", duplicate H1" if dup_h1 else "")
              + f". Recommendation: {v.get('recommendation')}")
    title = f"{v.get('verdict')}:{v.get('primary_keyword')}"[:80]
    record_issue(conn, source_id=source_id, url_id=a["row"]["url_id"], category="cannibalization",
                 severity=sev, title=title, detail=detail,
                 evidence=f"{a['row']['url']}  ⇄  {b['row']['url']}",
                 related_url_id=b["row"]["url_id"], detection_method="llm")
    record_issue(conn, source_id=source_id, url_id=b["row"]["url_id"], category="cannibalization",
                 severity=sev, title=title, detail=detail,
                 evidence=f"{b['row']['url']}  ⇄  {a['row']['url']}",
                 related_url_id=a["row"]["url_id"], detection_method="llm")
    return 2
