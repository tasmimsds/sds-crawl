# Evidence trimming — the fact sentence, not the paragraph

Evidence everywhere (UI cards, CSV, Excel, HTML report, FAQ findings) is now the **specific
sentence** containing the fact — max ~2 sentences, hard-capped at **300 chars** — and it always
contains the exact matched phrase. Full paragraphs are gone.

## How it works
- **`util.sentence_evidence(text, start, end)`** — extracts the sentence spanning the match offsets.
  Robust splitter: keeps decimals (`17.5 million`), handles list items, and non-Latin scripts
  (Japanese `。！？`, Hindi danda `।`). Adds one neighbour sentence only if the match sentence is
  very short (headings/list items). If a single sentence still exceeds 300 chars, or the source has
  **no** sentence delimiters (e.g. a scraped language-menu blob), it falls back to a ±120-char
  word-boundary window. The result always contains `text[start:end]`.
- **`util.trim_evidence(text, matched=None)`** — trims an already-captured string with no offsets
  (used for backfill). Anchors on `matched` if known, else the middle sentence (context windows were
  match-centred).
- **`issues.matched_value`** — new column storing the exact phrase to highlight. Populated at capture
  in every detector; recovered from `fact_matches` during backfill.

## Where it's wired at capture (sentence-anchored, exact offsets)
`analysis/facts.py`, `analysis/products.py`, `analysis/inventory.py` (`_claim_quote`),
`analysis/fact_check.py` (AI screen), `analysis/features.py`, `analysis/faqs.py` (the specific FAQ
answer sentence), `factcheck/query.py`, `factcheck/detect.py`. All also pass `matched_value`.

## Presentation
- **Results UI** (`results.html`): evidence rendered via the `highlight_match` Jinja filter (escapes,
  then wraps the matched phrase in `<mark>`). A **"show full context"** button hits
  `GET /issues/{id}/context`, which locates the phrase in the **stored** `crawl_results.body_text`
  (no re-crawl) and returns a ±400-char window — expanded inline, match highlighted.
- **CSV / Excel**: new `matched_value` column beside `evidence`; evidence is the trimmed sentence.
- **HTML report**: embeds `matched_value`; `hl()` helper marks the phrase.

## Backfill (one-time, no re-crawl)
`db.backfill_evidence(conn)` re-trimmed **395 / 744** legacy findings in place (the rest were already
short). Deterministic engines were then re-run (idempotent) to re-anchor their evidence on exact
offsets and populate `matched_value` — open count unchanged at **527** (no findings lost).

## Verification (all pass)
- 743 findings with evidence · **max length 300 · 0 over cap** · median 119.
- `matched_value` contained in its evidence: **386/386 (100%)**.
- CSV data rows **== XLSX == DB** for every category; `matched_value` column present; 0 evidence >300.
- Non-Latin pages (Hindi `SMB`, German umlauts/ß) render correctly and stay capped.
- "Show full context" expander: 98→840 chars from stored body, phrase highlighted, no re-crawl.
- Server-rendered `<mark>` highlight on 310/500 open evidence blocks (rest are AI-screen findings
  that get `matched_value` on the next full sync — LLM currently paused on key cap).
