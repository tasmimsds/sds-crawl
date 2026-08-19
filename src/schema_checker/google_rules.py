"""Google rich-result eligibility — the GOOGLE LAYER (principle #1: separate from schema.org
vocabulary compliance; principle #5: a versioned LOCAL ruleset, because there is no public
Rich Results Test API).

RULESET is hand-maintained from Google Search Central's structured-data docs: per rich-result
FEATURE, the `required` properties (missing → ERROR, disqualifies the feature) and `recommended`
properties (missing → WARNING, still eligible but weaker). Bump `version` and edit the tables
when Search Central changes — no other code changes needed.
"""
from __future__ import annotations

from .vocab import load

# ── versioned local ruleset (source: developers.google.com/search/docs/appearance/structured-data) ──
RULESET = {
    "version": "search-central-2025-07",
    "features": {
        "Product snippet": {
            "applies_to": ["Product"],
            "required": ["name"],
            "recommended": ["image", "description", "offers", "aggregateRating", "review", "brand", "sku"],
        },
        "Merchant offer": {
            "applies_to": ["Offer"],
            "required": ["price", "priceCurrency"],
            "recommended": ["availability", "priceValidUntil", "url", "itemCondition"],
            "any_of": [["price", "priceSpecification"]],  # price OR priceSpecification
        },
        "FAQ": {
            "applies_to": ["FAQPage"],
            "required": ["mainEntity"],
            "recommended": [],
            "nested": "faq",  # each mainEntity Question needs name + acceptedAnswer.text
        },
        "Article": {
            "applies_to": ["Article", "NewsArticle", "BlogPosting"],
            "required": ["headline"],
            "recommended": ["image", "datePublished", "dateModified", "author", "publisher"],
        },
        "Breadcrumb": {
            "applies_to": ["BreadcrumbList"],
            "required": ["itemListElement"],
            "recommended": [],
            "nested": "breadcrumb",  # each ListItem needs position + name + item
        },
        "Review snippet": {
            "applies_to": ["Review"],
            "required": ["itemReviewed", "reviewRating", "author"],
            "recommended": ["datePublished", "publisher"],
        },
        "Aggregate rating": {
            "applies_to": ["AggregateRating"],
            "required": ["ratingValue"],
            "recommended": ["reviewCount", "ratingCount", "bestRating"],
            "any_of": [["reviewCount", "ratingCount"]],
        },
        "Video": {
            "applies_to": ["VideoObject"],
            "required": ["name", "description", "thumbnailUrl", "uploadDate"],
            "recommended": ["duration", "contentUrl", "embedUrl"],
        },
        "Event": {
            "applies_to": ["Event"],
            "required": ["name", "startDate", "location"],
            "recommended": ["endDate", "offers", "image", "performer",
                            "eventStatus", "eventAttendanceMode"],
        },
        "Dataset": {
            "applies_to": ["Dataset"],
            "required": ["name", "description"],
            "recommended": ["creator", "distribution", "license", "temporalCoverage"],
        },
        "Organization / logo": {
            "applies_to": ["Organization"],
            "required": ["name", "url", "logo"],
            "recommended": ["sameAs", "contactPoint", "description"],
        },
    },
}


def _has(node: dict, prop: str) -> bool:
    v = node.get(prop)
    return v not in (None, "", [], {})


def _finding(sev, feature, prop, msg, path):
    return {"severity": sev, "feature": feature, "property": prop, "message": msg, "path": path}


def _nested_faq(node, path, feature) -> list[dict]:
    out = []
    ents = node.get("mainEntity") or []
    ents = ents if isinstance(ents, list) else [ents]
    if not ents:
        return [_finding("error", feature, "mainEntity", "FAQPage has no Question items.", path)]
    for i, q in enumerate(ents):
        if not isinstance(q, dict):
            continue
        if not _has(q, "name"):
            out.append(_finding("error", feature, "name", f"Question[{i}] missing 'name'.", f"{path}.mainEntity[{i}]"))
        ans = q.get("acceptedAnswer")
        text = (ans or {}).get("text") if isinstance(ans, dict) else None
        if not text:
            out.append(_finding("error", feature, "acceptedAnswer.text",
                                f"Question[{i}] missing acceptedAnswer.text.", f"{path}.mainEntity[{i}]"))
    return out


def _nested_breadcrumb(node, path, feature) -> list[dict]:
    out = []
    items = node.get("itemListElement") or []
    items = items if isinstance(items, list) else [items]
    for i, li in enumerate(items):
        if not isinstance(li, dict):
            continue
        for req in ("position", "name", "item"):
            if not _has(li, req) and not (req == "name" and _has(li.get("item", {}) if isinstance(li.get("item"), dict) else {}, "name")):
                out.append(_finding("warning", feature, req,
                                    f"ListItem[{i}] missing '{req}'.", f"{path}.itemListElement[{i}]"))
    return out


def check(nodes: list[dict]) -> dict:
    """For every Google-supported type present, report required/recommended findings and
    whether the feature currently QUALIFIES (no required missing). Returns
    {features: {feature: {present, qualifies, findings:[...]}}, findings:[...]}."""
    vocab = load()
    results, all_findings = {}, []
    for n in nodes:
        node, path, ntypes = n["node"], n["path"], set(n["types"])
        # include ancestors so a subtype (e.g. NewsArticle) matches an Article feature
        ancestry = set().union(*(vocab.ancestors(t) for t in ntypes)) if ntypes else set()
        for feature, rule in RULESET["features"].items():
            if not (set(rule["applies_to"]) & (ntypes | ancestry)):
                continue
            fr = results.setdefault(feature, {"present": True, "qualifies": True, "findings": []})
            # required (missing → error)
            for prop in rule["required"]:
                # skip a required prop that is covered by an any_of alternative
                alt = next((grp for grp in rule.get("any_of", []) if prop in grp), None)
                if alt and any(_has(node, p) for p in alt):
                    continue
                if not _has(node, prop):
                    f = _finding("error", feature, prop,
                                 f"Missing required property '{prop}' for {feature}.", path)
                    fr["findings"].append(f); all_findings.append(f); fr["qualifies"] = False
            # recommended (missing → warning)
            for prop in rule["recommended"]:
                if not _has(node, prop):
                    f = _finding("warning", feature, prop,
                                 f"Recommended property '{prop}' missing for {feature}.", path)
                    fr["findings"].append(f); all_findings.append(f)
            # nested structural checks
            nested = rule.get("nested")
            extra = _nested_faq(node, path, feature) if nested == "faq" else \
                    _nested_breadcrumb(node, path, feature) if nested == "breadcrumb" else []
            for f in extra:
                fr["findings"].append(f); all_findings.append(f)
                if f["severity"] == "error":
                    fr["qualifies"] = False
    return {"features": results, "findings": all_findings,
            "ruleset_version": RULESET["version"]}
