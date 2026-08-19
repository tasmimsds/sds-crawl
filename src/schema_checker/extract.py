"""Extractor (principle #2: extraction must be EXHAUSTIVE).

Pulls EVERY <script type="application/ld+json"> block — not only rich-result-eligible
types — parses each, flattens @graph into individually-typed nodes, and preserves the
verbatim source for the report. JSON-LD is first-class; Microdata/RDFa are out of scope
for extraction depth in v1 (we note their presence).
"""
from __future__ import annotations

import json

from selectolax.parser import HTMLParser


def _node_types(node: dict) -> list[str]:
    t = node.get("@type")
    if not t:
        return []
    return [str(x).split("/")[-1] for x in (t if isinstance(t, list) else [t])]


def _flatten(obj, out: list, path: str = "$") -> None:
    """Walk a parsed JSON-LD value; collect every dict that has an @type as a node,
    recording its JSON path (for error locations) and its type(s). Handles @graph, nested
    entities, and arrays."""
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(v, out, f"{path}[{i}]")
        return
    if not isinstance(obj, dict):
        return
    if "@graph" in obj:                         # a graph container: recurse into its nodes
        _flatten(obj["@graph"], out, f"{path}.@graph")
        # a graph wrapper may itself carry a @type (rare) — still record it
    if obj.get("@type"):
        out.append({"path": path, "types": _node_types(obj), "node": obj})
    for k, v in obj.items():                    # nested typed entities (e.g. Product.offers)
        if k in ("@type", "@graph"):
            continue
        if isinstance(v, (list, dict)):
            _flatten(v, out, f"{path}.{k}")


def extract(html: str) -> dict:
    """Return everything downstream needs:
      blocks:      per <script> block {index, raw, parsed|None, error}
      nodes:       flattened typed nodes {path, types, node} across all blocks
      types:       sorted distinct @type names found (exhaustive)
      block_count, parse_errors, has_microdata, has_rdfa
    """
    tree = HTMLParser(html or "")
    blocks, nodes, parse_errors = [], [], []
    for i, script in enumerate(tree.css('script[type="application/ld+json"]')):
        raw = script.text() or ""
        rec = {"index": i, "raw": raw.strip(), "parsed": None, "error": None}
        try:
            parsed = json.loads(raw)
            rec["parsed"] = parsed
            _flatten(parsed, nodes, f"block[{i}]")
        except (json.JSONDecodeError, ValueError) as exc:
            rec["error"] = f"JSON parse error: {exc}"
            parse_errors.append({"block": i, "message": rec["error"]})
        blocks.append(rec)

    types = sorted({t for n in nodes for t in n["types"]})
    return {
        "blocks": blocks,
        "nodes": nodes,
        "types": types,
        "block_count": len(blocks),
        "parse_errors": parse_errors,
        # secondary formats: presence only (v1 reads JSON-LD deeply, notes the rest)
        "has_microdata": bool(tree.css_first("[itemscope]")),
        "has_rdfa": bool(tree.css_first("[typeof], [vocab]")),
    }


def extract_snippet(text: str) -> dict:
    """Snippet mode: accept raw JSON-LD (a bare object/array) OR an HTML fragment that
    contains <script> blocks. Lets users pre-publish-test without a live URL."""
    text = (text or "").strip()
    if text[:1] in ("{", "["):                  # looks like bare JSON-LD
        html = f'<script type="application/ld+json">{text}</script>'
    else:
        html = text
    return extract(html)
