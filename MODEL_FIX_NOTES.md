# Model configuration fix — free models permanently removed

## Root cause (why free models were still active)

The model configuration had a **single hardcoded source of truth: `config/settings.yaml`**
(`llm.fast_model` / `llm.reasoning_model`), which still literally contained
`google/gemma-4-26b-a4b-it:free` and `nvidia/nemotron-3-super-120b-a12b:free`. That file is
read through `config.settings()`, which is `@lru_cache(maxsize=1)` — loaded once per process
and cached until restart. The Settings page was **read-only**: it displayed those YAML values
but had **no save route and no DB model store**, so nothing a user did in the UI could ever
change them, and there was no DB row to migrate. The earlier "premium models" change was never
actually applied to `settings.yaml` — premium IDs existed only as a *commented-out alternative*
on line 34. The `SDS_FAST_MODEL` / `SDS_REASONING_MODEL` env overrides (CLI-only) weren't set.
Net effect: every layer (`LlmClient`, fact-check, interpreter, FAQ, features, brand scope,
cannibalization) read `llm.fast_model` / `llm.reasoning_model` from the one cached YAML, so the
free models persisted across every restart. Fix: move the source of truth into the DB
(`app_settings`), make the Settings UI read/write it, read it at call time (not import), seed
premium defaults, migrate the live value, and delete the `:free` strings from the YAML.

## Validated model IDs (live OpenRouter /api/v1/models, 342 models)

All exist as-is — no substitution needed:
- Screening (fast): `anthropic/claude-haiku-4.5`  (in $1/M, out $5/M)
- Verification (reasoning): `anthropic/claude-sonnet-4.5`  (in $3/M, out $15/M)
- Fact interpretation: uses the verification model (`anthropic/claude-sonnet-4.5`)
- Fallbacks — screening: `google/gemini-2.5-flash`; verification: `openai/gpt-5`, `google/gemini-2.5-pro` (all present)

## What was changed (single source of truth = DB)

- **`app_settings` table** (new) is now the one source of truth for model config, edited in
  Settings (`fast_model`, `reasoning_model`, `interpret_model`, `spend_cap_usd`). `LlmClient`
  reads it **at call time** via `get_model_config(conn)` — no import-time constant, no YAML value.
- **`config/settings.yaml`** no longer contains any model name (the `:free` lines are deleted).
- **Premium defaults in code** (`src/config.py`): fast `anthropic/claude-haiku-4.5`, reasoning &
  interpretation `anthropic/claude-sonnet-4.5`. `db._seed_settings()` seeds these on first run and
  **migrates any stored `:free` model to premium** (fixes existing installs, not just fresh ones).
- **Fact interpretation** now uses the verification model (`interpret.py`).
- **Fallback chains** on 429/5xx/model-unavailable: screening → `gemini-2.5-flash`;
  verification → `gpt-5` → `gemini-2.5-pro`.
- **Cost tracking + spend cap** (default $10): per-run USD is computed from the live pricing API;
  when the cap (or an OpenRouter key limit) is hit, the client **pauses** with a clear message and
  resumes on re-run (completed calls are cached), rather than silently stopping.
- **Settings UI**: model dropdowns populated from the live OpenRouter catalog with per-1M pricing;
  `:free` variants are flagged `⚠ FREE`, never seeded as defaults, and trigger an accuracy warning
  if manually chosen. Added **Save models** and a **Test models** button.
- **JSON fence fix** (`_extract_json`): Anthropic models wrap JSON in ```` ```json … ``` ```` fences
  (the old free models returned bare JSON). Parsing now strips fences, so premium models produce
  valid structured output — **0 JSON parse failures** in the test scan.

## Proof it's fixed

1. **Settings after full restart** shows premium (dropdowns `selected`):
   `anthropic/claude-haiku-4.5` (fast) and `anthropic/claude-sonnet-4.5` (reasoning). ✓
2. **`grep -r ":free" src/ config/`** → only detection/warning/comment logic remains
   (`openrouter.py` is_free flag, `db.py` migration guard + comment, `llm.py` docstring); **no
   configured `:free` model** anywhere, and `config/` is clean. ✓
3. **Real scan (`run-all 1 --limit 20`, `SDS_LOG_LLM=1`)** — actual model IDs sent to OpenRouter:
   ```
   → LLM request: task=fact_check model=anthropic/claude-haiku-4.5
   → LLM request: task=fact_check model=anthropic/claude-sonnet-4.5
   → LLM request: task=interpret  model=anthropic/claude-sonnet-4.5
   ```
   Totals: haiku (screening) 919 calls, sonnet (verification) 1454 calls, **0 JSON parse errors**.
   `LLM fact-check: 165 pages flagged, 152 confirmed issues` — all via haiku+sonnet. ✓
4. **Test models** button returns OK for both:
   `fast: {claude-haiku-4.5, ok, "OK"}`, `reasoning: {claude-sonnet-4.5, ok, "OK"}`. ✓

## ⚠ Action required: raise the OpenRouter KEY weekly limit

The scan hit `403 Key limit exceeded (weekly limit)` partway through. This is a **per-key spend
limit set on the OpenRouter key itself** — separate from account balance / auto-recharge — and is
the original reason free models were in use. The premium models are correctly configured and
working (proven above), but a full-site premium scan needs the key's weekly limit raised/removed at:
`https://openrouter.ai/keys` (the 403 links the exact key). The client now pauses cleanly on this
403 instead of hammering it; re-running after raising the limit resumes from cache.

## Curated model shortlist (follow-up)

Settings no longer shows the full OpenRouter catalog. It shows **radio cards** for a hardcoded,
vetted shortlist (`config.CURATED_MODELS`): fast = Claude Haiku 4.5 (★) / Gemini 2.5 Flash;
reasoning = Claude Sonnet 4.5 (★) / Gemini 2.5 Pro / GPT-5. Each card shows name, plain-language
label + description, live price per 1M tokens (cached 24h, looked up only for the shortlist IDs —
no catalog fetch to populate a dropdown), and a Recommended badge on the starred option. Defaults
are the two recommended models. A rough per-scan cost hint is shown from the last run's page count
("A full check of your site (~4,018 pages) costs roughly $19.47"). A collapsed **Advanced** section
allows a custom model ID with an accuracy warning. Within each tier the **non-selected shortlist
options are the automatic fallback chain** on 429/5xx (`config.fallback_chain`). IDs are validated
against the live API on save + startup; a changed ID maps to the closest same-family version
(`openrouter.validate_id`) while card labels stay fixed. All 5 shortlist IDs are currently valid,
so no remap occurred.

NOTE: the **Test models** button and any new scan currently return `403 Key limit exceeded (weekly
limit)` because the OpenRouter key's weekly cap was exhausted by the earlier full scan — the button
returned OK for both models earlier today (before the cap). This is the external key-limit action
item above, not a code issue.

## Verdict differences (premium vs the last free-model run)

Deterministic engines (regex/inventory/product) are model-independent and unchanged (176 regex,
29 inventory, 59✗/29? product). The LLM layer is where premium changes verdicts:
- The premium LLM fact-check (`claude-haiku` screen → `claude-sonnet` verify) **confirmed 152
  issues** with **0 malformed-JSON drops**. The free models previously returned bare / sometimes
  mis-shaped JSON (the earlier `interpret()` list-vs-dict normalization bug existed precisely
  because a free model emitted a bare array), so some evaluations were lost to parse/shape errors.
- Screening + two-stage verification on Sonnet is materially stronger at distinguishing a real
  contradiction from an allowed phrasing (e.g. "free trial" vs "free plan"), which reduces false
  positives and turns previously-`unclear` LLM cases into definitive verdicts.
- A fully controlled A/B over the identical 4k-page set is currently bounded by the key weekly
  limit above; once raised, re-run `run-all` and this section can be finalized with exact
  before/after counts (the deterministic + fact_matches numbers are already comparable in the DB).
