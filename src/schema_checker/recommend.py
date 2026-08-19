"""Gap & Recommendation Engine (component 7) — the core advisor logic.

Cross-references what the page SHOULD have (derived from intent + content signals) against
what it HAS (extracted schema), and emits a per-page recommendation set with an action verb:
  KEEP        — right type present and valid
  UPGRADE     — a generic type present where a specific one fits (WebPage → Article)
  ADD         — a supported type is missing
  REMOVE/FIX  — invalid, duplicated, or content-unsupported markup

Principle #7 is enforced HERE: a type only enters `should_have` when its supporting content
signal is present. No signal → no recommendation → we never suggest markup for absent content.
"""
from __future__ import annotations

from .vocab import load

# generic containers that are fine to keep but are candidates to UPGRADE into something specific
_GENERIC = {"WebPage", "WebSite", "Thing", "CreativeWork"}
# types that are structurally fine to keep regardless of a specific content signal
_ALWAYS_OK = {"WebSite", "WebPage", "Organization", "BreadcrumbList", "ImageObject",
              "SearchAction", "ListItem", "Question", "Answer", "Offer", "ContactPoint",
              "PostalAddress", "Person", "Brand", "AggregateRating", "Rating"}

# intent → the specific page type it should carry (drives UPGRADE targets)
_INTENT_TYPE = {
    "article": "Article", "news": "NewsArticle", "case_study": "Article",
    "product": "Product", "pricing": "Product", "about": "AboutPage",
    "contact": "ContactPage", "home": "WebSite", "app_download": "SoftwareApplication",
    "collection": "CollectionPage",
}


def _should_have(intent: str, signals: dict) -> list[dict]:
    """Derive the target schema for this page. Each entry only appears when its content
    signal is present (principle #7). Returns [{type, rationale, signal, priority}]."""
    out: list[dict] = []

    def want(type_, rationale, signal, priority):
        out.append({"type": type_, "rationale": rationale, "signal": signal, "priority": priority})

    # intent-driven primary type
    primary = _INTENT_TYPE.get(intent)
    if primary:
        want(primary, f"Page intent is '{intent}'.", "intent", "HIGH")
    # case studies benefit from Review as well (acceptance criterion)
    if intent == "case_study":
        want("Review", "Case study reads as a customer review/outcome.", "intent", "MEDIUM")

    # signal-driven types (only when the signal is actually present)
    if signals["faq"]["present"]:
        want("FAQPage", f"Page has a visible FAQ ({signals['faq']['count']} Q/A pairs).", "faq", "HIGH")
    if signals["prices"] and intent in ("product", "pricing", "home"):
        want("Product", "On-page prices/plans detected.", "prices", "HIGH")
        want("Offer", "Prices detected — attach Offer(s) to the Product.", "prices", "MEDIUM")
    if signals["video"]:
        want("VideoObject", "Embedded video detected.", "video", "MEDIUM")
    if signals["breadcrumb"]:
        want("BreadcrumbList", "Breadcrumb navigation present.", "breadcrumb", "MEDIUM")
    if signals["contact"]["present"] and intent in ("contact", "about", "home"):
        want("Organization", "Contact details (phone/email/address) present.", "contact", "MEDIUM")
    if signals["app_links"]:
        want("SoftwareApplication", "App-store download links present.", "app_links", "MEDIUM")
    if signals["search_box"] and intent in ("home", "search_tool"):
        want("WebSite", "Site search present → WebSite + SearchAction (sitelinks searchbox).", "search_box", "LOW")
    return out


def recommend(intent: str, signals: dict, present_types: list[str],
              vocab_findings: list[dict]) -> list[dict]:
    """Produce the recommendation set. `present_types` = extract().types."""
    vocab = load()
    present = set(present_types)
    # types flagged invalid by the schema.org layer are REMOVE/FIX candidates
    invalid = {f["type"] for f in vocab_findings if f.get("code") == "unknown_type" and f.get("type")}
    have_generic = present & _GENERIC

    recs: list[dict] = []
    should = _should_have(intent, signals)
    should_types = {s["type"] for s in should}

    for s in should:
        t, why, prio = s["type"], s["rationale"], s["priority"]
        # already present in the correct specific form → KEEP
        if t in present:
            recs.append({"action": "KEEP", "type": t, "rationale": why, "priority": "Maintain"})
        # a generic container is present but the page is really `t` → UPGRADE
        elif have_generic and t not in _GENERIC and vocab.ancestors(t) & (have_generic | {"CreativeWork"}):
            recs.append({"action": "UPGRADE", "type": t,
                         "rationale": f"{why} Upgrade {sorted(have_generic)} → {t}.", "priority": prio})
        else:
            recs.append({"action": "ADD", "type": t, "rationale": why, "priority": prio})

    # present markup that no content signal supports (and isn't a structural helper) → REMOVE/FIX
    for t in present:
        if t in invalid:
            recs.append({"action": "REMOVE/FIX", "type": t,
                         "rationale": "Type is not valid schema.org — fix or remove.", "priority": "HIGH"})
        elif t not in should_types and t not in _ALWAYS_OK and t not in _GENERIC:
            recs.append({"action": "REMOVE/FIX", "type": t,
                         "rationale": "Present markup has no supporting page content — verify or remove "
                                      "(principle #7: markup without content risks a manual action).",
                         "priority": "MEDIUM"})
    return recs
