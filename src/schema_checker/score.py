"""Scorer (part of component 9). Two orthogonal 0–100 scores (principle #1 in numbers):

  Validity  — schema.org compliance: starts at 100, penalised by vocabulary errors/warnings
              and parse errors. "Is the markup correct?"
  Coverage  — intent match: how well the present schema matches what the page IS, derived
              from the recommendation set. "Does the page have the RIGHT schema?"

A page can score Validity 100 / Coverage 20 (valid but wrong schema) or vice-versa.
"""
from __future__ import annotations

# validity penalties
_V_PARSE = 40
_V_ERROR = 15
_V_WARN = 3
# coverage penalties by action + priority
_C_PENALTY = {
    ("ADD", "HIGH"): 25, ("ADD", "MEDIUM"): 12, ("ADD", "LOW"): 5,
    ("UPGRADE", "HIGH"): 20, ("UPGRADE", "MEDIUM"): 12, ("UPGRADE", "LOW"): 5,
    ("REMOVE/FIX", "HIGH"): 20, ("REMOVE/FIX", "MEDIUM"): 10, ("REMOVE/FIX", "LOW"): 4,
}


def validity_score(vocab_findings: list[dict], parse_errors: list[dict]) -> int:
    score = 100 - _V_PARSE * len(parse_errors)
    for f in vocab_findings:
        score -= _V_ERROR if f["severity"] == "error" else _V_WARN
    return max(0, min(100, score))


def coverage_score(recommendations: list[dict], had_any_schema: bool) -> int:
    if not recommendations and had_any_schema:
        return 100
    score = 100
    for r in recommendations:
        score -= _C_PENALTY.get((r["action"], r.get("priority", "MEDIUM")), 0)
    if not had_any_schema:
        score = min(score, 40)  # no schema at all caps coverage low
    return max(0, min(100, score))


def priority(validity: int, coverage: int, recommendations: list[dict]) -> str:
    """Overall fix priority for the page (HIGH / MEDIUM / LOW / Maintain)."""
    has_high = any(r.get("priority") == "HIGH" for r in recommendations
                   if r["action"] in ("ADD", "UPGRADE", "REMOVE/FIX"))
    if validity < 60 or has_high:
        return "HIGH"
    if coverage < 70:
        return "MEDIUM"
    if coverage < 90 or validity < 90:
        return "LOW"
    return "Maintain"
