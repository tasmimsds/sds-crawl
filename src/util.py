"""Small pure helpers: URL parsing, hashing, text context, union-find."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

_WS = re.compile(r"\s+")


_LOCALE_RE = re.compile(r"^[a-z]{2}$")


def parse_locale_section(url: str) -> tuple[str | None, str | None]:
    """Locale = path segment 1 IFF it is a 2-letter code; else no locale.

    Pattern: https://sdsmanager.com/{locale}/{section}/{slug}/
    Top-level pages like /about-us/ or /bulk-operations/ have no locale prefix,
    so segment 1 there is the section, not a locale. The 2-letter rule derives
    the locale set dynamically (us, uk, de, jp, cn, ...) without hardcoding it.
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return None, None
    segs = [s for s in path.split("/") if s]
    if segs and _LOCALE_RE.match(segs[0]):
        locale = segs[0]
        section = segs[1] if len(segs) >= 2 else None
    else:
        locale = None
        section = segs[0] if segs else None
    return locale, section


def host_of(url: str) -> str | None:
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def registrable_domain(url: str) -> str | None:
    """Best-effort eTLD+1 for grouping (handles sdsmanager.com / .no / .es)."""
    host = host_of(url)
    if not host:
        return None
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def word_count(text: str) -> int:
    t = normalize_text(text)
    return 0 if not t else len(t.split(" "))


def context_around(text: str, index: int, length: int, radius: int = 150) -> str:
    """Sentence-ish snippet around a match, trimmed to whitespace boundaries."""
    start = max(0, index - radius)
    end = min(len(text), index + length + radius)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + re.sub(r"^\S*\s", "", snippet)
    if end < len(text):
        snippet = re.sub(r"\s\S*$", "", snippet) + "…"
    return normalize_text(snippet)


class DSU:
    """Union-find over hashable keys (hreflang grouping)."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in list(self._parent.keys()):
            out.setdefault(self.find(key), []).append(key)
        return out
