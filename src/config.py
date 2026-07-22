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
