"""HTML -> structured content + FAQ extraction (visible + FAQPage JSON-LD)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from .util import content_type_of, normalize_text, sha256, word_count

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
    content_type: str = "other"          # blog | news | other (from URL path)
    author: str | None = None            # only for blog/news; else None
    author_status: str = "not_applicable"  # found | not_found | not_applicable


def _text(node) -> str:
    return normalize_text(node.text()) if node else ""


def _attr(tree, selector, attr) -> str | None:
    node = tree.css_first(selector)
    if not node:
        return None
    val = node.attributes.get(attr)
    return normalize_text(val) if val else None


def extract(html: str, url: str | None = None) -> Extracted:
    tree = HTMLParser(html)
    out = Extracted()

    # content type is path-derived; author only makes sense for blog/news pages
    out.content_type = content_type_of(url) if url else "other"
    if out.content_type in ("blog", "news"):
        out.author = _extract_author(tree)
        out.author_status = "found" if out.author else "not_found"
    else:
        out.author = None
        out.author_status = "not_applicable"

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


def extract_paragraph(html: str, needle: str, max_chars: int = 1200) -> str:
    """Full paragraph (block element) containing `needle` — the surrounding context of
    a backlink anchor or a brand mention. Prefers the smallest block (<p>/<li>/<td>/
    <blockquote>) that contains the text; falls back to a ±350-char window in body text.
    Returns the whole paragraph (NOT sentence-trimmed) so we see how they describe us."""
    if not html or not needle:
        return ""
    needle_l = normalize_text(needle).lower()
    if not needle_l:
        return ""
    tree = HTMLParser(html)
    for node in tree.css(_STRIP):
        node.decompose()
    best = ""
    for node in tree.css("p, li, td, blockquote, dd, figcaption"):
        txt = normalize_text(node.text())
        if txt and needle_l in txt.lower():
            # smallest block that still contains the needle = the tightest paragraph
            if not best or len(txt) < len(best):
                best = txt
    if best:
        return best[:max_chars]
    # fallback: window around the needle in the page body
    body = normalize_text((tree.css_first("body") or tree).text())
    i = body.lower().find(needle_l)
    if i < 0:
        return ""
    start = max(0, i - 350)
    end = min(len(body), i + len(needle) + 350)
    seg = body[start:end]
    if start > 0:
        seg = "…" + re.sub(r"^\S*\s", "", seg)
    if end < len(body):
        seg = re.sub(r"\s\S*$", "", seg) + "…"
    return seg[:max_chars]


# ---- Author extraction (blog/news only) -----------------------------------

_ARTICLE_TYPES = {"Article", "NewsArticle", "BlogPosting", "Report", "TechArticle"}
_BYLINE_SEL = (
    '[rel="author"], .author, .byline, .post-author, .article-author, .entry-author, '
    '.author-name, .writer, [class*="author"], [class*="byline"], [itemprop="author"]'
)
_BYLINE_PREFIX = re.compile(r"^\s*(?:by|written by|author|posted by|words by)\b[:\s]*", re.I)
# strip a trailing date/role after a separator: "Jane Doe | 19 Mar 2026", "Jane Doe — Editor",
# "Jane Doe, Senior Editor", "Jane Doe on March 2024"
_BYLINE_TAIL = re.compile(r"\s*(?:[|•·–—]|,|\bon\b).*$", re.I)


def _clean_author(name: str) -> str | None:
    name = normalize_text(name or "")
    name = _BYLINE_PREFIX.sub("", name)
    name = _BYLINE_TAIL.sub("", name).strip(" -–—|,·•")
    # guard against junk (empty, too long to be a name, or a leftover sentence)
    if not name or len(name) > 80:
        return None
    return name.strip() or None


def _authors_from_jsonld(author) -> list[str]:
    """author may be a string, an object with 'name', or a list of either."""
    out: list[str] = []
    if isinstance(author, list):
        for a in author:
            out.extend(_authors_from_jsonld(a))
    elif isinstance(author, dict):
        n = author.get("name")
        if n:
            out.append(str(n))
    elif isinstance(author, str):
        out.append(author)
    return out


def _walk_authors(node) -> list[str]:
    """Collect author names from any Article/NewsArticle/BlogPosting in a JSON-LD tree."""
    names: list[str] = []
    if isinstance(node, list):
        for item in node:
            names.extend(_walk_authors(item))
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t in _ARTICLE_TYPES for t in types) and node.get("author"):
            names.extend(_authors_from_jsonld(node["author"]))
        for key in ("@graph", "mainEntity", "hasPart", "itemListElement"):
            if key in node:
                names.extend(_walk_authors(node[key]))
    return names


def _extract_author(tree) -> str | None:
    """First strategy that yields a name wins: JSON-LD Article author, then author
    meta tags, then a visible byline. Returns None if nothing usable is found."""
    # 1) JSON-LD Article/NewsArticle/BlogPosting author.name
    for script in tree.css('script[type="application/ld+json"]'):
        raw = script.text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        names = [n for n in (_clean_author(x) for x in _walk_authors(data)) if n]
        if names:
            # de-dupe preserving order, join multiple authors
            seen, uniq = set(), []
            for n in names:
                if n.lower() not in seen:
                    seen.add(n.lower())
                    uniq.append(n)
            return "; ".join(uniq)

    # 2) meta tags
    for sel, attr in (
        ('meta[property="article:author"]', "content"),
        ('meta[property="og:article:author"]', "content"),
        ('meta[name="author"]', "content"),
        ('meta[property="author"]', "content"),
    ):
        node = tree.css_first(sel)
        if node:
            val = _clean_author(node.attributes.get(attr) or "")
            # skip URLs (article:author sometimes holds a profile link)
            if val and not val.lower().startswith(("http://", "https://", "/")):
                return val

    # 3) visible byline near the title
    for node in tree.css(_BYLINE_SEL):
        val = _clean_author(node.text())
        if val:
            return val
    # 3b) free-text "By {Name}" fallback in the first part of the article
    container = tree.css_first("article") or tree.css_first("main")
    if container:
        m = re.search(r"\b(?:By|Written by|Author)\b[:\s]+([A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+){0,3})",
                      container.text() or "")
        if m:
            return _clean_author(m.group(1))
    return None


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
