"""Content & Intent Analyzer (component 4) — the differentiator.

Parses the VISIBLE page and emits (a) a structured `content signals` object — the concrete,
debuggable evidence of what the page contains — and (b) a page `intent` from a controlled
taxonomy. Downstream, the Recommendation Engine only ever suggests a schema type whose
supporting signal is present (principle #7: never invent content).

Signals are heuristic and transparent by design. The intent taxonomy and the signal→intent
scoring are DATA (INTENT_RULES) so they can be tuned without changing logic.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

# controlled page-intent taxonomy (principle: a fixed vocabulary, not free text)
TAXONOMY = ["home", "product", "pricing", "interactive_tool", "search_tool", "article",
            "news", "case_study", "collection", "contact", "about", "app_download",
            "partner", "other"]

_PRICE_RE = re.compile(r"(?:[$£€]\s?\d[\d,]*(?:\.\d+)?)|(?:\b\d+\s?(?:/\s?mo|per month|/month|USD|EUR)\b)", re.I)
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "wistia", "loom.com")


def _signals(html: str, url: str, page) -> dict:
    """Concrete content signals. `page` is src.extractor.Extracted (faqs, h1s, h2s, body_text)."""
    tree = HTMLParser(html or "")
    body = (getattr(page, "body_text", "") or "").lower()
    h1s = getattr(page, "h1s", []) or []
    h2s = getattr(page, "h2s", []) or []
    faqs = getattr(page, "faqs", []) or []

    def any_css(sel):
        return bool(tree.css_first(sel))

    # video: <video>, or an iframe/anchor to a known video host
    video = any_css("video") or any(
        h in (n.attributes.get("src") or n.attributes.get("href") or "")
        for n in tree.css("iframe, a") for h in _VIDEO_HOSTS)
    # testimonials: blockquotes, or classes hinting reviews/testimonials
    testimonials = any_css("blockquote") or any_css('[class*="testimonial"], [class*="review"], [class*="quote"]')
    # contact: tel:/mailto: links, a form, an address block
    phone = any_css('a[href^="tel:"]')
    email = any_css('a[href^="mailto:"]')
    form = any_css("form")
    address = any_css("address") or bool(re.search(r"\b\d{4,6}\b.*\b(street|st|road|rd|ave|suite)\b", body))
    # app store links
    app_links = any(h in (n.attributes.get("href") or "")
                    for n in tree.css("a") for h in ("apps.apple.com", "play.google.com"))
    # how-it-works / steps: ordered list near a "how it works"/"steps" heading
    steps = ("how it works" in body or "step 1" in body or "step-by-step" in body) and any_css("ol")
    # feature list
    features = any(h.lower().startswith(("feature", "why ", "what you get", "benefits")) for h in h2s) or \
               ("features" in body[:4000] and any_css("ul"))
    # breadcrumb trail
    breadcrumb = any_css('nav[aria-label*="readcrumb"], ol[class*="readcrumb"], [class*="breadcrumb"]')
    # author/date (article signal)
    author = any_css('[rel="author"], [class*="author"], [itemprop="author"]')
    dateish = bool(re.search(r"\b20\d{2}\b", (h1s[0] if h1s else "") + " " + body[:600])) and author
    # real FAQ = 2+ Q/A pairs (from the extractor's visible+jsonld FAQ detection)
    faq_present = len(faqs) >= 2
    # interactive tool / search: search box, calculators, "try", "demo"
    search_box = any_css('input[type="search"], form[role="search"]')
    interactive = ("calculator" in body or "try it" in body or "demo" in body or "generate" in body) and any_css("input, button")
    # prices/plans
    prices = bool(_PRICE_RE.search(body)) or "pricing" in body[:3000]

    return {
        "h1": h1s[:3], "h2_outline": h2s[:12],
        "faq": {"present": faq_present, "count": len(faqs)},
        "prices": prices, "video": video, "testimonials": testimonials,
        "contact": {"phone": phone, "email": email, "form": form, "address": address,
                    "present": phone or email or (form and address)},
        "app_links": app_links, "steps": steps, "feature_list": features,
        "breadcrumb": breadcrumb, "author": author, "has_date": dateish,
        "search_box": search_box, "interactive": interactive,
    }


# signal/url → intent scoring. Each rule: (intent, weight, predicate(signals, url, title)).
def _kw(url, title, *words):
    hay = (url + " " + title).lower()
    return any(w in hay for w in words)


INTENT_RULES = [
    ("home",          3, lambda s, u, t: urlparse(u).path.strip("/") in ("", "index", "home")),
    ("pricing",       5, lambda s, u, t: _kw(u, t, "pricing", "plans", "/price") or (s["prices"] and _kw(u, t, "pricing"))),
    ("contact",       5, lambda s, u, t: _kw(u, t, "contact") or s["contact"]["present"]),
    ("about",         4, lambda s, u, t: _kw(u, t, "about", "about-us", "company")),
    ("app_download",  5, lambda s, u, t: s["app_links"] or _kw(u, t, "download", "mobile-app", "/app")),
    ("news",          4, lambda s, u, t: _kw(u, t, "news", "press", "chemical-hse-news")),
    ("case_study",    5, lambda s, u, t: _kw(u, t, "case-study", "case-studies", "success-story", "customer-story")),
    ("article",       3, lambda s, u, t: (s["author"] and s["has_date"]) or _kw(u, t, "blog", "article", "guide", "/articles")),
    ("search_tool",   4, lambda s, u, t: s["search_box"] or _kw(u, t, "search", "find-sds", "lookup")),
    ("interactive_tool", 4, lambda s, u, t: s["interactive"] or _kw(u, t, "tool", "calculator", "generator")),
    ("product",       4, lambda s, u, t: s["feature_list"] and (s["prices"] or _kw(u, t, "software", "platform", "product", "solution"))),
    ("partner",       4, lambda s, u, t: _kw(u, t, "partner", "affiliate", "reseller", "program")),
    ("collection",    3, lambda s, u, t: _kw(u, t, "category", "categories", "/hub", "resources") and not s["author"]),
]


def classify_intent(signals: dict, url: str, title: str) -> dict:
    scores: dict[str, int] = {}
    for intent, weight, pred in INTENT_RULES:
        try:
            if pred(signals, url, title or ""):
                scores[intent] = scores.get(intent, 0) + weight
        except Exception:  # noqa: BLE001 — a predicate must never break classification
            continue
    if not scores:
        return {"intent": "other", "confidence": 0.0, "scores": {}}
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    return {"intent": best, "confidence": round(scores[best] / total, 2), "scores": scores}


def analyze(html: str, url: str, page) -> dict:
    sig = _signals(html, url, page)
    title = getattr(page, "title", "") or ""
    intent = classify_intent(sig, url, title)
    return {"intent": intent["intent"], "confidence": intent["confidence"],
            "intent_scores": intent["scores"], "signals": sig}
