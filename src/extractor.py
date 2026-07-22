"""HTML -> structured content + FAQ extraction (visible + FAQPage JSON-LD)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from .util import normalize_text, sha256, word_count

_STRIP = "script, style, noscript, nav, header, footer, form, svg, iframe, aside"
_QUESTION_RE = re.compile(r"\?\s*$")


@dataclass
class FAQ:
    question: str
    answer: str
    source: str  # 'visible' | 'jsonld'


@dataclass
class Extracted:
    title: str | None = None
    meta_description: str | None = None
    canonical: str | None = None
    meta_robots: str | None = None
    h1s: list[str] = field(default_factory=list)
    h2s: list[str] = field(default_factory=list)
    body_text: str = ""
    word_count: int = 0
    content_hash: str = ""
    faqs: list[FAQ] = field(default_factory=list)


def _text(node) -> str:
    return normalize_text(node.text()) if node else ""


def _attr(tree, selector, attr) -> str | None:
    node = tree.css_first(selector)
    if not node:
        return None
    val = node.attributes.get(attr)
    return normalize_text(val) if val else None


def extract(html: str) -> Extracted:
    tree = HTMLParser(html)
    out = Extracted()

    title_node = tree.css_first("title")
    out.title = _text(title_node) or None
    out.meta_description = _attr(tree, 'meta[name="description"]', "content")
    out.canonical = _attr(tree, 'link[rel="canonical"]', "href")
    out.meta_robots = _attr(tree, 'meta[name="robots"]', "content")
    out.h1s = [t for t in (_text(n) for n in tree.css("h1")) if t]
    out.h2s = [t for t in (_text(n) for n in tree.css("h2")) if t]

    out.faqs = _extract_faqs(html, tree)

    # Body: strip chrome then prefer <main>/<article>, else largest block.
    body_tree = HTMLParser(html)
    for node in body_tree.css(_STRIP):
        node.decompose()
    container = body_tree.css_first("main") or body_tree.css_first("article")
    if container is None:
        container = _largest_block(body_tree)
    if container is None:
        container = body_tree.css_first("body")
    out.body_text = normalize_text(container.text()) if container else ""
    out.word_count = word_count(out.body_text)
    out.content_hash = sha256(out.body_text)
    return out


def _largest_block(tree):
    best = None
    best_len = 0
    for node in tree.css("div, section"):
        length = len(normalize_text(node.text()))
        if length > best_len:
            best_len, best = length, node
    return best


# ---- FAQ extraction -------------------------------------------------------

def _extract_faqs(html: str, tree) -> list[FAQ]:
    faqs: list[FAQ] = []
    seen: set[tuple[str, str]] = set()

    def add(q: str, a: str, src: str):
        q, a = normalize_text(q), normalize_text(_strip_html(a))
        if not q or not a:
            return
        key = (q.lower(), src)
        if key in seen:
            return
        seen.add(key)
        faqs.append(FAQ(question=q, answer=a, source=src))

    # 1) FAQPage JSON-LD
    for script in tree.css('script[type="application/ld+json"]'):
        raw = script.text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for q, a in _walk_jsonld(data):
            add(q, a, "jsonld")

    # 2) visible <details>/<summary>
    for det in tree.css("details"):
        summ = det.css_first("summary")
        if summ:
            q = summ.text()
            summ.decompose()
            add(q, det.text(), "visible")

    # 3) <dl><dt>Q</dt><dd>A</dd>
    for dl in tree.css("dl"):
        dts = dl.css("dt")
        dds = dl.css("dd")
        for dt, dd in zip(dts, dds):
            add(dt.text(), dd.text(), "visible")

    return faqs


def _walk_jsonld(node) -> list[tuple[str, str]]:
    """Find Question objects anywhere in a JSON-LD structure."""
    found: list[tuple[str, str]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld(item))
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t == "Question" for t in types):
            name = node.get("name") or node.get("question") or ""
            answer = node.get("acceptedAnswer") or node.get("suggestedAnswer") or {}
            if isinstance(answer, list):
                answer = answer[0] if answer else {}
            text = answer.get("text", "") if isinstance(answer, dict) else str(answer)
            if name:
                found.append((str(name), str(text)))
        for key in ("mainEntity", "@graph", "itemListElement", "hasPart"):
            if key in node:
                found.extend(_walk_jsonld(node[key]))
    return found


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")
