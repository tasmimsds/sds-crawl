"""Shared helpers for the LLM analysis passes."""
from __future__ import annotations

import asyncio

from ..config import settings
from ..rules import load_features as load_features_db
from ..rules import load_rules


def facts_context(conn) -> str:
    lines = []
    for f in load_rules(conn):
        cur = f.get("current_value") or f.get("canonical_value") or "TBD"
        line = f"- [{f['id']} / {f['category']}] {f.get('description','')}. Correct value: {cur}."
        if f.get("allowed_patterns"):
            line += " ALLOWED (not an issue): " + "; ".join(f["allowed_patterns"][:4]) + "."
        lines.append(line)
    return "\n".join(lines) if lines else "(none)"


def features_context(conn) -> str:
    lines = []
    for ft in load_features_db(conn):
        lines.append(
            f"- [{ft['id']}] {ft['name']} — status={ft['status']}. {ft.get('description','')}"
            + (f" NOTE: {ft['notes']}" if ft.get("notes") else "")
        )
    return "\n".join(lines) if lines else "(none)"


def english_pages(conn, source_id: int, all_locales: bool):
    english = set(settings()["llm"]["english_locales"])
    rows = conn.execute(
        """SELECT u.id AS url_id, u.url, u.locale, c.content_hash, c.body_text
           FROM urls u
           JOIN crawl_results c ON c.id = (
             SELECT id FROM crawl_results WHERE url_id=u.id ORDER BY id DESC LIMIT 1
           )
           WHERE u.source_id=? AND c.body_text IS NOT NULL AND c.body_text != ''""",
        (source_id,),
    ).fetchall()
    if all_locales:
        return rows
    return [r for r in rows if r["locale"] is None or r["locale"] in english]


async def gather_limited(coro_factories, limit: int):
    """Run async callables with bounded concurrency; return results in order."""
    sem = asyncio.Semaphore(limit)
    results = [None] * len(coro_factories)

    async def run(i, factory):
        async with sem:
            results[i] = await factory()

    await asyncio.gather(*(run(i, f) for i, f in enumerate(coro_factories)))
    return results
