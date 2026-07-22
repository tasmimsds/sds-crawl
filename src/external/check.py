"""External Fact Check — search the web (Bright Data SERP) for what other sites
say about SDS Manager, then check those claims against our facts."""
from __future__ import annotations

import asyncio
import json

from ..analysis.llm import LlmClient
from ..db import now_iso
from ..factcheck.query import verdict
from ..util import sha256
from .serp import serp_search

BRAND = "SDS Manager"
_OWN = ("sdsmanager",)


def _external(rows):
    seen, out = set(), []
    for r in rows:
        d = (r.get("domain") or "").lower()
        if any(o in d for o in _OWN):
            continue  # skip our own domains — we want EXTERNAL sites
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        out.append(r)
    return out


async def _gather_serp(queries) -> list[dict]:
    results = []
    for q in queries:
        try:
            results.extend(await asyncio.to_thread(serp_search, q))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! SERP query failed ({q}): {exc}")
    return _external(results)


def _clear(conn, source_id, kind):
    conn.execute("DELETE FROM external_findings WHERE source_id=? AND kind=?", (source_id, kind))


def _store(conn, source_id, kind, fact_name, query, r, verdict_val, reason, expected):
    conn.execute(
        """INSERT INTO external_findings
             (source_id, kind, fact_name, query, external_url, domain, title, snippet,
              verdict, reason, expected, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
        (source_id, kind, fact_name, query, r["url"], r.get("domain"), r.get("title"),
         r.get("snippet"), verdict_val, reason, expected, now_iso()),
    )


async def run_external_fact(conn, source_id: int, fact: dict, clear: bool = True) -> dict:
    """Search the web for the fact and check external claims vs our correct value."""
    terms = (fact.get("search_terms") or [])[:3]
    topic = fact.get("claim_topic") or fact.get("fact_name") or ""
    correct = (fact.get("correct_value") or "").strip()
    queries = [f'{BRAND} {t}' for t in terms] + [f"{BRAND} {topic}".strip()]
    queries = list(dict.fromkeys([q for q in queries if q.strip()]))[:4]

    rows = await _gather_serp(queries)
    counts = {"mismatch": 0, "matches": 0, "unclear": 0, "unrelated": 0}
    if correct and rows:
        await verdict(conn, rows, correct)  # sets row["verdict"]/["verdict_reason"]
        for r in rows:
            counts[r.get("verdict") or "unclear"] = counts.get(r.get("verdict") or "unclear", 0) + 1
    else:
        for r in rows:
            r["verdict"] = None

    if clear:
        _clear(conn, source_id, "factcheck")
    for r in rows:
        _store(conn, source_id, "factcheck", fact.get("fact_name"), " / ".join(queries),
               r, r.get("verdict"), r.get("verdict_reason"), correct or None)
    conn.commit()
    return {"fact": fact, "queries": queries, "rows": rows, "counts": counts,
            "total": len(rows)}


async def run_external_for_saved_rules(conn, source_id: int, limit: int = 3) -> int:
    """External-check the top saved fact rules that have a correct value (bounded)."""
    from ..rules import load_rules

    rules = [r for r in load_rules(conn) if r.get("current_value")][:limit]
    _clear(conn, source_id, "factcheck")
    total = 0
    for r in rules:
        fact = {"fact_name": r["id"], "claim_topic": r.get("description") or r["id"],
                "correct_value": r["current_value"], "search_terms": r.get("search_terms") or []}
        res = await run_external_fact(conn, source_id, fact, clear=False)
        total += res["total"]
    return total


_MENTION_SYS = (
    "You analyze how third-party web pages talk about the company SDS Manager. For each "
    "snippet, give sentiment (positive|neutral|negative) toward SDS Manager and a one-line "
    "summary of what the page says about it. Respond ONLY with JSON."
)


async def run_external_mentions(conn, source_id: int) -> dict:
    queries = [BRAND, f"{BRAND} review", f"{BRAND} vs", f"{BRAND} alternative",
               f'"{BRAND}" SDS software']
    rows = await _gather_serp(queries)
    llm = LlmClient(conn)
    counts = {"positive": 0, "neutral": 0, "negative": 0}

    if llm.enabled and rows:
        for start in range(0, len(rows), 10):
            chunk = rows[start:start + 10]
            payload = [{"i": start + k, "title": c["title"], "text": c["snippet"]}
                       for k, c in enumerate(chunk)]
            data = await llm.call_json(
                task="ext_mention", model=llm.fast_model,
                cache_key=sha256(json.dumps(payload)) + "|mention", system=_MENTION_SYS,
                user=("Snippets (JSON):\n" + json.dumps(payload, ensure_ascii=False) +
                      '\n\nReturn JSON {"items":[{"i":<index>,"sentiment":"positive|neutral|negative",'
                      '"summary":"one line"}]}.'),
            )
            by_i = {v["i"]: v for v in (data or {}).get("items", [])}
            for k, c in enumerate(chunk):
                v = by_i.get(start + k) or {}
                c["verdict"] = v.get("sentiment", "neutral")
                c["verdict_reason"] = v.get("summary", "")
    else:
        for r in rows:
            r["verdict"], r["verdict_reason"] = "neutral", ""

    for r in rows:
        counts[r.get("verdict", "neutral")] = counts.get(r.get("verdict", "neutral"), 0) + 1

    _clear(conn, source_id, "mention")
    for r in rows:
        _store(conn, source_id, "mention", None, " / ".join(queries), r,
               r.get("verdict"), r.get("verdict_reason"), None)
    conn.commit()
    return {"queries": queries, "rows": rows, "counts": counts, "total": len(rows)}
