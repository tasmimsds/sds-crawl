"""Small pure helpers: URL parsing, hashing, text context, union-find."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

_WS = re.compile(r"\s+")


_LOCALE_RE = re.compile(r"^[a-z]{2}$")


def content_type_of(url: str) -> str:
    """Classify a page from its URL path (region-agnostic): the region segment
    (/us/, /uk/, /au/, …) is NOT part of the test — the type marker is matched
    anywhere in the path.
      contains '/sds-management-articles/' -> 'blog'
      contains '/chemical-hse-news/'       -> 'news'
      everything else                      -> 'other'
    Only blog/news pages carry an author; 'other' pages never do."""
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return "other"
    segs = [s for s in path.split("/") if s]
    if "sds-management-articles" in segs:
        return "blog"
    if "chemical-hse-news" in segs:
        return "news"
    return "other"


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


def host_excluded(url: str, exclude_hosts) -> bool:
    """True if the URL's host is in exclude_hosts (exact or a subdomain of one).
    Used to keep internal panels like admin55.sdsmanager.com out of all crawling."""
    if not exclude_hosts:
        return False
    host = (host_of(url) or "").lower()
    if not host:
        return False
    for e in exclude_hosts:
        e = (e or "").lower().lstrip(".")
        if host == e or host.endswith("." + e):
            return True
    return False


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


# ── Sentence-level evidence extraction ──────────────────────────────────────
# Goal: evidence = the SPECIFIC sentence containing the fact (max ~2 sentences,
# hard cap ~300 chars), always containing the matched phrase — never a whole
# paragraph. Handles decimals ("17.5 million"), abbreviations, list items, and
# non-Latin scripts (Japanese 。！？, Hindi danda ।).
_MAX_EVIDENCE = 300
# A boundary is: a run of Latin enders .!?… FOLLOWED by whitespace/end (so a
# decimal like 17.5 — ender followed by a digit — is never a boundary), OR a
# CJK/Devanagari ender which terminates on its own (never used inside numbers).
_BOUNDARY = re.compile(r"[.!?…]+(?=\s|$)|[。！？।]+")


def _boundaries(text: str) -> list[int]:
    """Offsets just AFTER each sentence-ending punctuation run."""
    ends: list[int] = []
    for m in _BOUNDARY.finditer(text):
        i = m.start()
        # "17. 5" style: ender is a bare '.' with a digit right before and the
        # next non-space char a digit -> treat as decimal, not a boundary.
        if text[i] == "." and i > 0 and text[i - 1].isdigit():
            tail = text[m.end():m.end() + 3].lstrip()
            if tail[:1].isdigit():
                continue
        ends.append(m.end())
    return ends


def _window(text: str, start: int, end: int, radius: int) -> str:
    """Fallback: ±radius chars around the match, trimmed to word boundaries."""
    n = len(text)
    w0 = max(0, start - radius)
    w1 = min(n, end + radius)
    s = text[w0:w1]
    if w0 > 0:
        s = re.sub(r"^\S*\s", "", s)
    if w1 < n:
        s = re.sub(r"\s\S*$", "", s)
    pre = "…" if w0 > 0 else ""
    post = "…" if w1 < n else ""
    return normalize_text(pre + s + post)


def sentence_evidence(text: str, start: int, end: int, max_chars: int = _MAX_EVIDENCE) -> str:
    """The sentence(s) of `text` spanning [start, end) — the match's own
    sentence, plus one neighbour only if the match sentence is very short.
    Always contains text[start:end]. Caps at max_chars, falling back to a
    ±120-char word-boundary window if a single sentence is still too long."""
    text = text or ""
    n = len(text)
    if n == 0:
        return ""
    start = max(0, min(start, n))
    end = max(start, min(end, n))
    ends = _boundaries(text)
    lo = 0
    for e in ends:
        if e <= start:
            lo = e
        else:
            break
    hi = n
    for e in ends:
        if e >= end:
            hi = e
            break
    sent = text[lo:hi].strip()
    matched = text[start:end]
    if matched and matched not in sent:  # safety: never drop the fact
        return _window(text, start, end, 120)
    # very short match sentence (e.g. a heading / list item) -> add one neighbour
    if len(sent) < 40 and hi < n:
        nxt = n
        for e in ends:
            if e > hi:
                nxt = e
                break
        cand = text[lo:nxt].strip()
        if len(cand) <= max_chars:
            sent = cand
    if len(sent) > max_chars:
        return _window(text, start, end, 120)
    return normalize_text(sent)


def trim_evidence(text: str, matched: str | None = None, max_chars: int = _MAX_EVIDENCE) -> str:
    """Trim an already-captured evidence string (no source offsets) down to the
    fact sentence. Used to backfill legacy paragraph-length evidence. If the
    matched phrase is known, anchors on it; otherwise assumes the match was
    roughly centred (how context_around built the window) and picks the middle
    sentence."""
    text = (text or "").strip()
    if not text:
        return ""
    core = text.strip("…").strip()  # drop context_around's ellipses
    if len(core) <= max_chars and len(_boundaries(core)) <= 1:
        return normalize_text(core)  # already a single short sentence
    if matched and matched in core:
        i = core.index(matched)
        return sentence_evidence(core, i, i + len(matched), max_chars)
    mid = len(core) // 2
    return sentence_evidence(core, mid, min(mid + 1, len(core)), max_chars)


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
