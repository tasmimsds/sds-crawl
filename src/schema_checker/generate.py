"""Code Generator (component 8) — corrected, deploy-ready JSON-LD.

Consolidates everything into a SINGLE @graph, de-duplicates shared entities (one canonical
Organization referenced by @id), and fills required + recommended fields from real content
signals where available. Where a human must supply a value it emits an explicit PLACEHOLDER —
it NEVER fabricates prices, ratings, or reviews (principle #7/#8). Output is clean JSON-LD:
no comments (principle #8), both pretty and minified.
"""
from __future__ import annotations

import json

PLACEHOLDER = "REPLACE_ME"


def _ph(what: str) -> str:
    return f"{PLACEHOLDER}: {what}"


def _org_id(base: str) -> str:
    return f"{base.rstrip('/')}/#organization"


def _canonical_org(existing_orgs: list[dict], brand: dict, base_url: str) -> tuple[dict, list[str]]:
    """One Organization node from all present Org markup + the project brand. Flags
    cross-node inconsistencies (name/logo/sameAs) instead of silently picking one."""
    names = {(o.get("name") or "").strip() for o in existing_orgs if o.get("name")}
    logos = {(o.get("logo", {}).get("url") if isinstance(o.get("logo"), dict) else o.get("logo"))
             for o in existing_orgs if o.get("logo")}
    sameas = sorted({s for o in existing_orgs for s in
                     (o.get("sameAs") if isinstance(o.get("sameAs"), list) else [o.get("sameAs")]) if s})
    name = (brand.get("brand_name") or (sorted(names)[0] if names else None) or _ph("Organization name"))
    org = {"@type": "Organization", "@id": _org_id(base_url), "name": name,
           "url": base_url or _ph("homepage URL"),
           "logo": (sorted(logos)[0] if logos else _ph("absolute logo URL"))}
    if sameas:
        org["sameAs"] = sameas
    warnings = []
    if len(names) > 1:
        warnings.append(f"Inconsistent Organization name across markup: {sorted(names)} — unified to '{name}'.")
    if len([l for l in logos if l]) > 1:
        warnings.append(f"Inconsistent Organization logo across markup: {sorted(l for l in logos if l)}.")
    return org, warnings


def _first(nodes: list[dict], type_: str) -> dict:
    for n in nodes:
        if type_ in (n["types"] or []):
            return dict(n["node"])
    return {}


def _build_node(type_: str, existing: dict, signals: dict, page, url: str, org_ref: dict) -> dict:
    """Build/repair one node of `type_`, filling from existing markup + real signals, with
    explicit placeholders for human-required values. Never fabricates prices/ratings/reviews."""
    node = {k: v for k, v in existing.items() if k not in ("@context",)}
    node["@type"] = type_
    title = getattr(page, "title", "") or ""
    h1 = (getattr(page, "h1s", []) or [""])[0]

    if type_ in ("Article", "NewsArticle", "BlogPosting"):
        node.setdefault("headline", h1 or title or _ph("article headline (≤110 chars)"))
        node.setdefault("author", {"@type": "Person", "name": _ph("author name")} if not signals["author"]
                        else {"@type": "Person", "name": _ph("author name from byline")})
        node.setdefault("datePublished", _ph("publish date ISO-8601, e.g. 2026-01-31"))
        node.setdefault("image", _ph("absolute featured image URL"))
        node["publisher"] = {"@id": org_ref["@id"]}
        node.setdefault("mainEntityOfPage", url or _ph("page URL"))
    elif type_ == "Product":
        node.setdefault("name", h1 or title or _ph("product name"))
        node.setdefault("description", _ph("product description"))
        node.setdefault("brand", {"@id": org_ref["@id"]})
        # prices detected but we DO NOT invent the number — placeholder Offer
        node.setdefault("offers", {"@type": "Offer", "price": _ph("numeric price, no currency symbol"),
                                   "priceCurrency": _ph("ISO 4217 code, e.g. USD"),
                                   "availability": "https://schema.org/InStock"})
    elif type_ == "Offer":
        node = {"@type": "Offer", "price": _ph("numeric price"), "priceCurrency": _ph("ISO 4217 code")}
    elif type_ == "FAQPage":
        faqs = getattr(page, "faqs", []) or []
        node["mainEntity"] = [
            {"@type": "Question", "name": f.question,
             "acceptedAnswer": {"@type": "Answer", "text": f.answer}}
            for f in faqs] or [{"@type": "Question", "name": _ph("question"),
                                "acceptedAnswer": {"@type": "Answer", "text": _ph("answer")}}]
    elif type_ == "VideoObject":
        node.setdefault("name", h1 or _ph("video title"))
        node.setdefault("description", _ph("video description"))
        node.setdefault("thumbnailUrl", _ph("absolute thumbnail URL"))
        node.setdefault("uploadDate", _ph("upload date ISO-8601"))
    elif type_ == "SoftwareApplication":
        node.setdefault("name", title or h1 or _ph("app name"))
        node.setdefault("applicationCategory", _ph("e.g. BusinessApplication"))
        node.setdefault("operatingSystem", _ph("e.g. iOS, Android, Web"))
        node.setdefault("offers", {"@type": "Offer", "price": _ph("price or 0"),
                                   "priceCurrency": _ph("ISO 4217 code")})
    elif type_ == "Review":
        # NEVER fabricate a rating — leave the human to supply it
        node.setdefault("itemReviewed", {"@id": org_ref["@id"]})
        node.setdefault("reviewRating", {"@type": "Rating", "ratingValue": _ph("1–5 rating"),
                                         "bestRating": "5"})
        node.setdefault("author", {"@type": "Person", "name": _ph("reviewer / customer name")})
    elif type_ == "BreadcrumbList":
        node.setdefault("itemListElement", existing.get("itemListElement") or [
            {"@type": "ListItem", "position": 1, "name": _ph("crumb name"), "item": _ph("crumb URL")}])
    elif type_ == "WebSite":
        node.setdefault("name", (org_ref.get("name") if org_ref else None) or title or _ph("site name"))
        node.setdefault("url", url or _ph("homepage URL"))
        if signals.get("search_box"):
            node.setdefault("potentialAction", {
                "@type": "SearchAction",
                "target": {"@type": "EntryPoint", "urlTemplate": _ph("https://site/search?q={search_term_string}")},
                "query-input": "required name=search_term_string"})
    node.pop("@context", None)
    return node


def generate(url: str, page, signals: dict, extracted: dict, recommendations: list[dict],
             brand: dict | None = None) -> dict:
    """Return {pretty, minified, graph, org_warnings}. Emits ONE @graph, deduped Organization."""
    brand = brand or {}
    base = (url or "").split("?")[0] or _ph("homepage URL")
    existing_nodes = extracted["nodes"]
    existing_orgs = [n["node"] for n in existing_nodes if "Organization" in (n["types"] or [])]

    org, org_warnings = _canonical_org(existing_orgs, brand, base)
    graph: list[dict] = [org]
    seen_types = {"Organization"}

    # every KEEP/ADD/UPGRADE recommendation becomes a node (REMOVE/FIX ones are intentionally dropped)
    for rec in recommendations:
        if rec["action"] not in ("KEEP", "ADD", "UPGRADE"):
            continue
        t = rec["type"]
        if t in ("Organization", "Offer") or t in seen_types:
            continue  # Organization already canonical; Offer nested under Product
        node = _build_node(t, _first(existing_nodes, t), signals, page, url, org)
        node.setdefault("@id", f"{base.rstrip('/')}/#{t.lower()}")
        graph.append(node)
        seen_types.add(t)

    doc = {"@context": "https://schema.org", "@graph": graph}
    pretty = json.dumps(doc, indent=2, ensure_ascii=False)
    minified = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    # principle #8: JSON-LD has no comments — assert none slipped in
    assert "//" not in pretty.replace("://", ""), "generated JSON-LD must contain no comments"
    return {"pretty": pretty, "minified": minified, "graph": graph, "org_warnings": org_warnings}
