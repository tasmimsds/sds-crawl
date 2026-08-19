"""Vocabulary Validator — the schema.org LAYER (principle #1: kept strictly separate from
Google eligibility). Checks each extracted type/property against the loaded vocabulary:
unknown types, unknown/misspelled properties, datatype mismatches vs rangeIncludes, and
properties used outside their domainIncludes. Emits errors/warnings with JSON-path locations.

Severity model (principle #6): error = disqualifying (unknown type, clear datatype
contradiction); warning = compliance nit (unknown property, misplaced property).
"""
from __future__ import annotations

from .vocab import Vocabulary, _localname

# schema.org literal datatypes (a value here should be a scalar, not a typed object)
LITERAL_TYPES = {
    "Text", "URL", "Number", "Integer", "Float", "Boolean", "Date", "DateTime",
    "Time", "CssSelectorType", "XPathType", "PronounceableText", "DataType",
}
_KEYWORDS = {"@type", "@id", "@context", "@graph", "@value", "@language", "@reverse"}


def _finding(sev, code, msg, path, prop=None, type_=None):
    return {"severity": sev, "code": code, "message": msg, "path": path,
            "property": prop, "type": type_}


def _is_reference(value) -> bool:
    """{'@id': '...'} with nothing else = a link to another node, valid for object ranges."""
    return isinstance(value, dict) and set(value) <= {"@id", "@type"} and "@id" in value


def _datatype_finding(vocab: Vocabulary, prop: str, value, path: str, type_: str):
    """Compare a value against the property's rangeIncludes. Returns a finding or None.

    JSON-LD ambiguity is handled explicitly: a string can satisfy an object range as an
    @id reference; an object with @type can satisfy a literal range only if it's clearly
    wrong. We only flag the UNAMBIGUOUS contradictions to avoid false positives."""
    ranges = vocab.ranges(prop)
    if not ranges:
        return None
    literal_range = ranges & LITERAL_TYPES
    object_range = ranges - LITERAL_TYPES
    values = value if isinstance(value, list) else [value]
    for v in values:
        v_is_object = isinstance(v, dict) and not _is_reference(v) and "@value" not in v
        if v_is_object and not object_range:
            # a typed object supplied where only a literal (Text/Number/…) is allowed
            return _finding("error", "datatype_mismatch",
                            f"'{prop}' expects {sorted(ranges)} but got a nested object.",
                            path, prop, type_)
        if isinstance(v, bool) and "Boolean" not in ranges and literal_range:
            return _finding("warning", "datatype_mismatch",
                            f"'{prop}' got a boolean but expects {sorted(ranges)}.", path, prop, type_)
    return None


def validate(nodes: list[dict], vocab: Vocabulary) -> list[dict]:
    """Validate every flattened typed node. `nodes` come from extract().nodes."""
    findings: list[dict] = []
    for n in nodes:
        node, path = n["node"], n["path"]
        node_types = [t for t in n["types"]]
        # 1) unknown types
        for t in node_types:
            if t and not vocab.is_type(t):
                findings.append(_finding(
                    "error", "unknown_type",
                    f"'{t}' is not a recognized schema.org type.", path, type_=t))
        primary = next((t for t in node_types if vocab.is_type(t)), node_types[0] if node_types else None)
        primary_known = bool(primary and vocab.is_type(primary))
        # 2) properties
        for key, value in node.items():
            if key in _KEYWORDS or ":" in key:      # keywords / external-namespace props
                continue
            prop = _localname(key)
            if not vocab.is_property(prop):
                findings.append(_finding(
                    "warning", "unknown_property",
                    f"'{prop}' is not a recognized schema.org property.", path, prop, primary))
                continue
            # 3) misplaced property (used outside its domainIncludes) — skip when the type
            #    itself is unknown (we already errored on it; every prop would look misplaced)
            if primary_known and not vocab.property_allowed_on(prop, primary):
                findings.append(_finding(
                    "warning", "misplaced_property",
                    f"'{prop}' is not declared for type '{primary}' "
                    f"(domainIncludes: {sorted(vocab.domains(prop))}).", path, prop, primary))
            # 4) datatype vs rangeIncludes
            dt = _datatype_finding(vocab, prop, value, f"{path}.{key}", primary)
            if dt:
                findings.append(dt)
    return findings
