"""OpenRouter LLM client: async, JSON-mode, SQLite-cached, cost-capped."""
from __future__ import annotations

import json
import os

from ..config import openrouter_key, settings
from ..db import get_cached_llm, put_cached_llm


class LlmClient:
    def __init__(self, conn):
        self.conn = conn
        s = settings()["llm"]
        self.cfg = s
        # Per-run overrides (set by CLI --fast-model / --reasoning-model) win
        # over settings.yaml.
        self.fast_model = os.getenv("SDS_FAST_MODEL") or s["fast_model"]
        self.reasoning_model = os.getenv("SDS_REASONING_MODEL") or s["reasoning_model"]
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
                base_url=s["base_url"],
                api_key=key,
                default_headers={
                    "HTTP-Referer": os.getenv("OPENROUTER_APP_URL", ""),
                    "X-Title": os.getenv("OPENROUTER_APP_TITLE", "SDS Internal Audit"),
                },
            )

    async def call_json(self, *, task: str, model: str, cache_key: str, system: str, user: str):
        cached = get_cached_llm(self.conn, cache_key, task, model)
        if cached is not None:
            try:
                return json.loads(cached)
            except json.JSONDecodeError:
                pass
        if not self._client:
            return None
        if self.calls >= self.max_calls:
            print(f"  ! LLM call cap ({self.max_calls}) reached; skipping further calls.")
            return None
        self.calls += 1
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                temperature=self.temperature,
                max_tokens=self.max_output,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            content = resp.choices[0].message.content or ""
            if resp.usage:
                self.prompt_tokens += resp.usage.prompt_tokens or 0
                self.completion_tokens += resp.usage.completion_tokens or 0
            data = json.loads(content)
            put_cached_llm(self.conn, cache_key, task, model, content)
            return data
        except Exception as exc:  # noqa: BLE001
            print(f"  ! LLM call failed ({task}/{model}): {exc}")
            return None

    def log_usage(self):
        print(f"LLM usage: {self.calls} live calls, "
              f"~{self.prompt_tokens} prompt + {self.completion_tokens} completion tokens.")
