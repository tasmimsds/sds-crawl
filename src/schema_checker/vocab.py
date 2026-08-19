"""schema.org vocabulary — loaded from the official machine-readable dump, NOT hardcoded.

Principle #4: vocabulary is DATA. We download `schemaorg-current-https.jsonld`, cache it,
and refresh on a schedule so new schema.org releases flow through automatically. Everything
the Vocabulary Validator and Code Generator need (valid types, valid properties, each
property's domainIncludes/rangeIncludes, and the class hierarchy via subClassOf) comes from
here — change the dump, not the code.
"""
from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import PROJECT_ROOT

_DUMP_URL = "https://schema.org/version/latest/schemaorg-current-https.jsonld"
_CACHE = PROJECT_ROOT / "data" / "schema_vocab" / "schemaorg-current-https.jsonld"
_REFRESH_DAYS = 30  # schema.org ships a few releases a year


def _localname(node) -> str:
    """'schema:FAQPage' / {'@id':'schema:FAQPage'} -> 'FAQPage'. Handles str, dict, xsd:*."""
    if isinstance(node, dict):
        node = node.get("@id", "")
    return str(node).split(":")[-1] if node else ""


def _as_list(v) -> list:
    """domainIncludes/rangeIncludes/subClassOf may be a single dict OR a list of dicts."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


@dataclass
class Vocabulary:
    types: set[str] = field(default_factory=set)                 # valid class names
    properties: dict[str, dict] = field(default_factory=dict)    # prop -> {domains, ranges}
    subclass_of: dict[str, set[str]] = field(default_factory=dict)  # type -> direct parents
    fetched_at: str | None = None

    # ---- type queries ----
    def is_type(self, name: str) -> bool:
        return _localname(name) in self.types

    def ancestors(self, name: str) -> set[str]:
        """All superclasses (transitive) of a type, including itself."""
        name = _localname(name)
        seen, stack = set(), [name]
        while stack:
            t = stack.pop()
            if t in seen:
                continue
            seen.add(t)
            stack.extend(self.subclass_of.get(t, ()))
        return seen

    # ---- property queries ----
    def is_property(self, name: str) -> bool:
        return _localname(name) in self.properties

    def domains(self, prop: str) -> set[str]:
        return set(self.properties.get(_localname(prop), {}).get("domains", ()))

    def ranges(self, prop: str) -> set[str]:
        return set(self.properties.get(_localname(prop), {}).get("ranges", ()))

    def property_allowed_on(self, prop: str, type_name: str) -> bool:
        """Is `prop` declared for `type_name` or any of its ancestors (domainIncludes)?"""
        doms = self.domains(prop)
        if not doms:                      # property with no declared domain → allow anywhere
            return True
        return bool(doms & self.ancestors(type_name))


def _parse(dump: dict) -> Vocabulary:
    v = Vocabulary(fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for node in dump.get("@graph", []):
        kinds = _as_list(node.get("@type"))
        kinds = {_localname(k) for k in kinds}
        name = _localname(node.get("@id"))
        if not name:
            continue
        if "Class" in kinds:              # rdfs:Class
            v.types.add(name)
            parents = {_localname(p) for p in _as_list(node.get("rdfs:subClassOf")) if _localname(p)}
            if parents:
                v.subclass_of.setdefault(name, set()).update(parents)
        if "Property" in kinds:           # rdf:Property
            v.properties[name] = {
                "domains": [_localname(d) for d in _as_list(node.get("schema:domainIncludes"))],
                "ranges": [_localname(r) for r in _as_list(node.get("schema:rangeIncludes"))],
            }
    return v


def refresh(force: bool = False) -> dict:
    """(Re)download the dump to the local cache if missing/stale. Returns status."""
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    fresh = _CACHE.exists() and (time.time() - _CACHE.stat().st_mtime) < _REFRESH_DAYS * 86400
    if fresh and not force:
        return {"refreshed": False, "cached": True, "path": str(_CACHE)}
    req = urllib.request.Request(_DUMP_URL, headers={"User-Agent": "ExactFact-SchemaChecker"})
    data = urllib.request.urlopen(req, timeout=60).read()
    _CACHE.write_bytes(data)
    _reset_cache()
    return {"refreshed": True, "cached": False, "path": str(_CACHE), "bytes": len(data)}


_loaded: Vocabulary | None = None


def _reset_cache() -> None:
    global _loaded
    _loaded = None


def load() -> Vocabulary:
    """Cached in-process vocabulary; downloads the dump on first use if absent."""
    global _loaded
    if _loaded is not None:
        return _loaded
    if not _CACHE.exists():
        refresh(force=True)
    _loaded = _parse(json.loads(_CACHE.read_text(encoding="utf-8")))
    return _loaded
