"""External mismatch detection — FUTURE SCOPE. Do NOT implement yet.

This is the documented extension point for the second analysis scope. Where the
*internal* passes (facts/positioning/free/counts/features/faqs)
check SDS Manager pages against our own source-of-truth registry and against each
other, the *external* scope will check SDS Manager's claims against sources
OUTSIDE our control — e.g. regulatory/standards text (OSHA, REACH, CLP/GHS
revisions), competitor pages, or authoritative references — to catch claims that
are internally consistent but externally wrong or outdated.

Requirements for this module will be provided later. Keep the interface below
stable so the CLI, DB (`issues` with a future category such as
`external_mismatch`), and reports can wire it in without touching other modules.

Expected integration points when built:
  * a new CLI command `analyze external <source>`
  * new issue category `external_mismatch` (add to db.CATEGORIES)
  * reuse the existing crawl_results.body_text + issues table + llm_cache
"""
from __future__ import annotations


async def analyze_external(conn, source_id: int, **kwargs) -> int:
    """Detect mismatches between SDS Manager claims and external sources.

    Args:
        conn: open SQLite connection (see src/db.py).
        source_id: the watched source to analyze.
        **kwargs: reserved for future options (e.g. reference source sets).

    Returns:
        Number of issues recorded (category `external_mismatch`).

    Not implemented — awaiting requirements.
    """
    raise NotImplementedError(
        "External mismatch detection is not implemented yet. "
        "Requirements will be provided later; see module docstring for the interface."
    )
