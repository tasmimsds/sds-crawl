"""Live OpenRouter model catalog + pricing (for the Settings dropdown and cost tracking)."""
from __future__ import annotations

import time

import httpx

from .config import openrouter_key

_CACHE: dict = {"at": 0.0, "models": []}
_TTL = 86400  # 24h — we only need pricing/validation for the curated shortlist


def list_models(force: bool = False) -> list[dict]:
    """All OpenRouter models: [{id, name, prompt, completion, is_free}], sorted by id.
    Cached in-process for 30 min. Returns [] if the key is missing or the call fails."""
    if not force and _CACHE["models"] and (time.time() - _CACHE["at"] < _TTL):
        return _CACHE["models"]
    key = openrouter_key()
    if not key:
        return _CACHE["models"] or []
    try:
        r = httpx.get("https://openrouter.ai/api/v1/models",
                      headers={"Authorization": f"Bearer {key}"}, timeout=30)
        r.raise_for_status()
        out = []
        for m in r.json().get("data", []):
            p = m.get("pricing") or {}
            prompt = float(p.get("prompt") or 0)
            completion = float(p.get("completion") or 0)
            mid = m.get("id", "")
            out.append({"id": mid, "name": m.get("name") or mid,
                        "prompt": prompt, "completion": completion,
                        "is_free": mid.endswith(":free") or (prompt == 0 and completion == 0)})
        out.sort(key=lambda x: x["id"])
        _CACHE.update(at=time.time(), models=out)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! OpenRouter models fetch failed: {exc}")
    return _CACHE["models"] or []


def _price(model_id: str) -> tuple[float, float]:
    for m in list_models():
        if m["id"] == model_id:
            return m["prompt"], m["completion"]
    return 0.0, 0.0


def cost_usd(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Dollar cost of a call. OpenRouter prices are per-token."""
    pin, pout = _price(model_id)
    return (prompt_tokens or 0) * pin + (completion_tokens or 0) * pout


def prices_for(ids: list[str]) -> dict:
    """{model_id: {'prompt_m': $/1M, 'completion_m': $/1M}} for the given (shortlist) IDs."""
    out = {}
    for mid in ids:
        pin, pout = _price(mid)
        out[mid] = {"prompt_m": round(pin * 1_000_000, 2), "completion_m": round(pout * 1_000_000, 2)}
    return out


def validate_id(model_id: str) -> tuple[str, bool]:
    """Confirm a model id exists in the live catalog. If not, map to the closest current
    version of the same family (same provider + shared name tokens). Returns (resolved_id,
    remapped?). Falls back to the original id if the catalog is unavailable."""
    models = list_models()
    if not models:
        return model_id, False
    ids = {m["id"] for m in models}
    if model_id in ids:
        return model_id, False
    provider = model_id.split("/", 1)[0]
    tokens = {t for t in model_id.split("/")[-1].replace("-", " ").split() if not t.replace(".", "").isdigit()}
    best, best_score = None, 0
    for m in models:
        if m["is_free"] or not m["id"].startswith(provider + "/"):
            continue
        cand = {t for t in m["id"].split("/")[-1].replace("-", " ").split()}
        score = len(tokens & cand)
        if score > best_score:
            best, best_score = m["id"], score
    if best and best_score:
        print(f"  ℹ model id '{model_id}' not in live catalog — mapped to '{best}' (same family).")
        return best, True
    return model_id, False
