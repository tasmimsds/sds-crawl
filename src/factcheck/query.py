"""Phase 3 — query-based fact search.

query -> LLM term expansion (editable) -> FTS5 retrieval -> highlighted snippets.
(No LLM match/mismatch verdicts yet — that's Phase 4.)
"""
from __future__ import annotations

import json
import re

from ..analysis.llm import LlmClient
from ..db import FTS_ENABLED
from ..util import context_around, sha256

_TOKEN = re.compile(r'[^\w\s+.,%$-]', re.UNICODE)

_SYS = (
    "You turn a user's fact-search query about a company's website into keyword search "
    "terms for a full-text index. Include synonyms, alternate phrasings, and numeric "
    "format variants (e.g. '17 million', '17M', '17,000,000', '17 mill'). "
    "Respond ONLY with JSON."
)


def _clean_term(t: str) -> str:
    return _TOKEN.sub(" ", t or "").strip()


async def expand_query(conn, q: str) -> list[str]:
    """Return search terms for the query (LLM if available, else the raw query)."""
    q = q.strip()
    if not q:
        return []
    llm = LlmClient(conn)
    terms: list[str] = []
    if llm.enabled:
        data = await llm.call_json(
            task="query_expand", model=llm.fast_model, cache_key=sha256(q) + "|qx",
            system=_SYS,
            user=(f'Query: "{q}"\nReturn JSON {{"terms":["...", "..."]}} with 4-10 concise '
                  f'search terms/phrases (each 1-4 words). Include the key nouns/numbers.'),
        )
        terms = [_clean_term(t) for t in (data or {}).get("terms", []) if _clean_term(t)]
    # always include the raw query words as a fallback / anchor
    if q not in terms:
        terms.insert(0, _clean_term(q))
    # dedupe, cap
    seen, out = set(), []
    for t in terms:
        k = t.lower()
        if t and k not in seen:
            seen.add(k)
            out.append(t)
    return out[:10]


def _build_match(terms: list[str]) -> str:
    """Build an FTS5 MATCH expression: OR of quoted phrases."""
    parts = []
    for t in terms:
        t = t.replace('"', " ").strip()
        if t:
            parts.append(f'"{t}"')
    return " OR ".join(parts)


def search(conn, source_id: int, terms: list[str], limit: int = 200) -> dict:
    """Run FTS retrieval scoped to a source; return highlighted snippets per page."""
    if not terms:
        return {"total": 0, "rows": [], "match": ""}
    match = _build_match(terms)
    if not match:
        return {"total": 0, "rows": [], "match": ""}

    if FTS_ENABLED:
        rows = conn.execute(
            """SELECT u.id AS url_id, u.url, u.locale, f.title,
                      snippet(content_fts, 6, '<mark>', '</mark>', '…', 32) AS snip,
                      f.body_text AS body
               FROM content_fts f JOIN urls u ON u.id = f.url_id
               WHERE u.source_id = ? AND content_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (source_id, match, limit),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) c FROM content_fts f JOIN urls u ON u.id=f.url_id "
            "WHERE u.source_id=? AND content_fts MATCH ?",
            (source_id, match),
        ).fetchone()["c"]
    else:  # LIKE fallback
        like = terms[0]
        rows = conn.execute(
            """SELECT u.id AS url_id, u.url, u.locale, c.title,
                      substr(c.body_text, 1, 240) AS snip, c.body_text AS body
               FROM urls u JOIN crawl_results c ON c.id=(
                 SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1)
               WHERE u.source_id=? AND c.body_text LIKE ? LIMIT ?""",
            (source_id, f"%{like}%", limit),
        ).fetchall()
        total = len(rows)

    out = []
    for r in rows:
        body = r["body"] or ""
        # context = sentence(s) around the FIRST matched term (for display + verdict)
        ctx = _term_context(body, terms) or (r["snip"] or "")
        out.append({"url_id": r["url_id"], "url": r["url"], "locale": r["locale"],
                    "title": r["title"], "snippet": ctx})
    return {"total": total, "match": match, "rows": out}


def _term_context(body: str, terms: list[str]) -> str:
    """First occurrence of any term in body -> ±context, with the term marked."""
    if not body:
        return ""
    low = body.lower()
    best = None
    for t in terms:
        i = low.find(t.lower())
        if i >= 0 and (best is None or i < best[0]):
            best = (i, len(t))
    if best is None:
        return ""
    ctx = context_around(body, best[0], best[1], 150)
    # highlight the matched term
    return re.sub("(" + re.escape(body[best[0]:best[0] + best[1]]) + ")",
                  r"<mark>\1</mark>", ctx, count=1, flags=re.I)


# ---- Phase 4: verdicts, query history, query -> issues ----

_MARK = re.compile(r"</?mark>")
_VERDICT_SYS = (
    "You judge whether each page snippet correctly states a given fact. "
    "matches = the snippet agrees with the correct value; mismatch = it states a "
    "different/outdated value; unclear = it doesn't actually state this fact. "
    "Respond ONLY with JSON."
)


def save_query(conn, query_text: str, terms: list[str], correct_value: str) -> int:
    """Upsert a query into history; return its id."""
    from ..db import now_iso

    row = conn.execute(
        "SELECT id FROM queries WHERE query_text=? AND IFNULL(correct_value,'')=?",
        (query_text, correct_value or ""),
    ).fetchone()
    if row:
        conn.execute("UPDATE queries SET last_run=?, expanded_terms=? WHERE id=?",
                     (now_iso(), json.dumps(terms), row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        """INSERT INTO queries (query_text, expanded_terms, correct_value, category, created_at, last_run)
           VALUES (?,?,?,?,?,?)""",
        (query_text, json.dumps(terms), correct_value or None, "other_mismatch",
         now_iso(), now_iso()),
    )
    conn.commit()
    return cur.lastrowid


async def verdict(conn, rows: list[dict], correct_value: str) -> dict:
    """Classify each matched snippet vs the correct value (batched LLM call)."""
    llm = LlmClient(conn)
    counts = {"matches": 0, "mismatch": 0, "unclear": 0, "unrelated": 0}
    if not llm.enabled or not correct_value.strip():
        for r in rows:
            r["verdict"] = None
        return {"counts": counts, "llm": llm.enabled}

    # small chunks so each JSON reply fits within max_output_tokens
    for start in range(0, len(rows), 10):
        chunk = rows[start:start + 10]
        payload = [{"i": start + k, "text": _MARK.sub("", c["snippet"])}
                   for k, c in enumerate(chunk)]
        key = sha256(correct_value + "|" + json.dumps(payload)) + "|vd"
        data = await llm.call_json(
            task="verdict", model=llm.reasoning_model, cache_key=key, system=_VERDICT_SYS,
            user=(f'Correct value: "{correct_value}".\nSnippets (JSON):\n'
                  f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                  'Return JSON {"verdicts":[{"i":<index>,"verdict":"matches|mismatch|unrelated|unclear",'
                  '"reason":"short"}]}. Use "unrelated" if the snippet is not about this fact at all.'),
        )
        by_i = {v["i"]: v for v in (data or {}).get("verdicts", [])}
        for k, c in enumerate(chunk):
            v = by_i.get(start + k)
            c["verdict"] = v["verdict"] if v else "unclear"
            c["verdict_reason"] = (v or {}).get("reason", "")
    for r in rows:
        counts[r.get("verdict") or "unclear"] = counts.get(r.get("verdict") or "unclear", 0) + 1
    return {"counts": counts, "llm": True}


def record_query_issues(conn, source_id: int, query_id: int, query_text: str,
                        correct_value: str, rows: list[dict]) -> int:
    """Write mismatches from a verdict pass as issues (category other_mismatch)."""
    from ..db import record_issue

    n = 0
    for r in rows:
        if r.get("verdict") != "mismatch" or not r.get("url_id"):
            continue
        record_issue(
            conn, source_id=source_id, url_id=r["url_id"], category="other_mismatch",
            severity="high", title=f"query:{query_text[:48]}",
            detail=r.get("verdict_reason") or f"Contradicts expected value: {correct_value}",
            evidence=_MARK.sub("", r["snippet"]), expected=correct_value,
            detection_method="llm",
        )
        # link the issue to the query
        conn.execute(
            """UPDATE issues SET query_id=? WHERE source_id=? AND url_id=? AND category='other_mismatch'
               AND title=?""",
            (query_id, source_id, r["url_id"], f"query:{query_text[:48]}"),
        )
        n += 1
    conn.commit()
    return n
