"""Configuration loading: settings.yaml, facts.yaml, features.yaml, and .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_PLACEHOLDER_KEYS = {"", "REPLACE_ME", None}

# Premium model defaults — NEVER free. The live source of truth is the DB
# (app_settings, edited in Settings); these are only the seed + ultimate fallback.
# Accuracy is the priority (account has auto-recharge), so free models are never seeded.
DEFAULT_FAST_MODEL = "anthropic/claude-haiku-4.5"        # screening (first curated recommended)
DEFAULT_REASONING_MODEL = "anthropic/claude-sonnet-4.5"  # verification + fact interpretation
DEFAULT_SPEND_CAP_USD = 10.0

# Curated shortlist shown in Settings as radio cards (NOT the full OpenRouter catalog).
# 2–3 vetted, affordable-but-effective models per tier; strong at reading content, reasoning,
# and clean structured output. No :free models. The non-selected options in a tier form that
# tier's automatic fallback chain (on 429/5xx). Card labels are fixed; IDs are validated live.
CURATED_MODELS = {
    "fast": [
        {"id": "anthropic/claude-haiku-4.5", "name": "Claude Haiku 4.5", "recommended": True,
         "label": "Recommended — best balance",
         "desc": "Best accuracy for classification + strict JSON at low cost."},
        {"id": "google/gemini-2.5-flash", "name": "Gemini 2.5 Flash", "recommended": False,
         "label": "Budget — good for very large sites",
         "desc": "Cheapest solid option, very fast, huge context."},
    ],
    "reasoning": [
        {"id": "anthropic/claude-sonnet-4.5", "name": "Claude Sonnet 4.5", "recommended": True,
         "label": "Recommended — most accurate",
         "desc": "Strongest judgment on nuanced verdicts (free trial vs free plan, brand disambiguation)."},
        {"id": "google/gemini-2.5-pro", "name": "Gemini 2.5 Pro", "recommended": False,
         "label": "Value — slightly cheaper",
         "desc": "Cheaper alternative, strong reasoning."},
        {"id": "openai/gpt-5", "name": "GPT-5", "recommended": False,
         "label": "Alternative",
         "desc": "Alternative flagship."},
    ],
}


def curated_ids(tier: str) -> list[str]:
    return [m["id"] for m in CURATED_MODELS.get(tier, [])]


def fallback_chain(tier: str, selected: str) -> list[str]:
    """Selected model first, then the other curated options in that tier (auto-fallback)."""
    chain, seen = [], set()
    for mid in [selected, *curated_ids(tier)]:
        if mid and mid not in seen:
            seen.add(mid); chain.append(mid)
    return chain


@lru_cache(maxsize=1)
def settings() -> dict:
    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def facts() -> list[dict]:
    with open(PROJECT_ROOT / "config" / "facts.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("facts", [])


@lru_cache(maxsize=1)
def features() -> list[dict]:
    with open(PROJECT_ROOT / "config" / "features.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("features", [])


def resolve_path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def openrouter_key() -> str | None:
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    return None if key in _PLACEHOLDER_KEYS or key.startswith("REPLACE") else key


def brightdata() -> dict:
    """Bright Data SERP proxy config from the environment."""
    return {
        "username": (os.getenv("BRIGHT_DATA_USERNAME") or "").strip(),
        "password": (os.getenv("BRIGHT_DATA_PASSWORD") or "").strip(),
        "host": (os.getenv("BRIGHT_DATA_PROXY_HOST") or "brd.superproxy.io").strip(),
        "port": (os.getenv("BRIGHT_DATA_PROXY_PORT") or "33335").strip(),
        "verify_ssl": (os.getenv("BRIGHT_DATA_VERIFY_SSL") or "false").strip().lower() == "true",
        "results": int(os.getenv("SERP_RESULTS_PER_QUERY") or 20),
        "location": (os.getenv("SERP_LOCATION") or "us").strip(),
        "language": (os.getenv("SERP_LANGUAGE") or "en").strip(),
    }


def serp_enabled() -> bool:
    bd = brightdata()
    return bool(bd["username"] and bd["password"])
