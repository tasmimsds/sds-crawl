"""OpenRouter LLM client: async, JSON-mode, SQLite-cached, cost-tracked, cap-guarded.

Model config comes from the DB (app_settings) at call time — the single source of truth
edited in Settings. No :free defaults, no settings.yaml model values, no import-time cache.
Each tier has a fallback chain tried on 429 / 5xx / model-unavailable.
"""
from __future__ import annotations

import json
import os
import re

# Anthropic models often wrap JSON in ```json … ``` fences; strip them before parsing.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def _extract_json(content: str) -> str:
    s = (content or "").strip()
    m = _FENCE_RE.search(s)
    if m:
        return m.group(1).strip()
    # fallback: slice from first bracket to last (handles stray prose)
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = s.find(open_c), s.rfind(close_c)
        if 0 <= i < j:
            return s[i:j + 1]
    return s

from ..config import fallback_chain, openrouter_key, settings
from ..db import get_cached_llm, get_model_config, put_cached_llm
from ..openrouter import cost_usd

# log the actual model id sent on each live request (proof + debugging)
LOG_REQUESTS = os.getenv("SDS_LOG_LLM", "").lower() in ("1", "true", "yes")

_RETRYABLE = ("429", "500", "502", "503", "529", "rate limit", "overloaded",
              "unavailable", "not a valid model", "no endpoints", "timeout", "timed out")


def _retryable(exc) -> bool:
    code = getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 529):
        return True
    s = str(exc).lower()
    return any(t in s for t in _RETRYABLE)


class LlmClient:
    def __init__(self, conn):
        self.conn = conn
        s = settings()["llm"]
        self.cfg = s
        mc = get_model_config(conn)  # DB is authoritative, read fresh (not import-cached)
        # env override is CLI-only (SDS_FAST_MODEL / SDS_REASONING_MODEL)
        self.fast_model = os.getenv("SDS_FAST_MODEL") or mc["fast_model"]
        self.reasoning_model = os.getenv("SDS_REASONING_MODEL") or mc["reasoning_model"]
        self.interpret_model = os.getenv("SDS_REASONING_MODEL") or mc["interpret_model"]
        self.spend_cap = mc["spend_cap_usd"]
        self.spend = 0.0
        self.paused = False
        self.max_calls = s["max_calls_per_run"]
        self.max_body = s["max_body_chars"]
        self.max_output = s.get("max_output_tokens", 2000)
        self.temperature = s["temperature"]
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        key = openrouter_key()
        self.enabled = bool(key)
        self._client = None
        if key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                base_url=s["base_url"], api_key=key,
                default_headers={
                    "HTTP-Referer": os.getenv("OPENROUTER_APP_URL", ""),
                    "X-Title": os.getenv("OPENROUTER_APP_TITLE", "SDS Internal Audit"),
                },
            )

    def _chain(self, model: str) -> list[str]:
        # non-selected curated options in the tier are the automatic fallback chain
        if model == self.fast_model:
            return fallback_chain("fast", model)
        if model in (self.reasoning_model, self.interpret_model):
            return fallback_chain("reasoning", model)
        return [model]

    async def call_json(self, *, task: str, model: str, cache_key: str, system: str, user: str):
        # cache is keyed by the PRIMARY (configured) model id, so changing models
        # invalidates old (e.g. free-model) verdicts automatically.
        cached = get_cached_llm(self.conn, cache_key, task, model)
        if cached is not None:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass
        if not self._client:
            return None
        if self.paused or self.spend >= self.spend_cap:
            if not self.paused:
                self.paused = True
                print(f"  ⏸ Spend cap ${self.spend_cap:.2f} reached (spent ${self.spend:.4f}). "
                      f"Pausing LLM calls — raise the cap in Settings and re-run to resume "
                      f"(completed calls are cached, so it continues where it stopped).")
            return None
        if self.calls >= self.max_calls:
            print(f"  ! LLM call cap ({self.max_calls}) reached; skipping further calls.")
            return None

        last_exc = None
        for m in self._chain(model):
            self.calls += 1
            if LOG_REQUESTS:
                print(f"  → LLM request: task={task} model={m}")
            try:
                resp = await self._client.chat.completions.create(
                    model=m, temperature=self.temperature, max_tokens=self.max_output,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                )
                content = _extract_json(resp.choices[0].message.content or "")
                if resp.usage:
                    pt = resp.usage.prompt_tokens or 0
                    ct = resp.usage.completion_tokens or 0
                    self.prompt_tokens += pt
                    self.completion_tokens += ct
                    self.spend += cost_usd(m, pt, ct)
                data = json.loads(content)
                put_cached_llm(self.conn, cache_key, task, model, content)  # store clean JSON
                return data
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                s = str(exc).lower()
                # An OpenRouter KEY limit (403) applies to every model on this key —
                # fallbacks won't help. Pause so we don't hammer it thousands of times.
                if "key limit" in s or getattr(exc, "status_code", None) == 403:
                    self.paused = True
                    print(f"  ⏸ OpenRouter key limit hit — pausing. Raise the key's spend "
                          f"limit in the OpenRouter dashboard, then re-run (cached calls resume). {exc}")
                    return None
                if _retryable(exc) and m != self._chain(model)[-1]:
                    print(f"  ! {m} failed ({exc}); falling back to next model…")
                    continue
                print(f"  ! LLM call failed ({task}/{m}): {exc}")
                return None
        print(f"  ! all fallbacks exhausted for {task}: {last_exc}")
        return None

    def log_usage(self):
        print(f"LLM usage: {self.calls} live calls, "
              f"~{self.prompt_tokens} prompt + {self.completion_tokens} completion tokens, "
              f"est. ${self.spend:.4f}" + (" (PAUSED: spend cap hit)" if self.paused else ""))
