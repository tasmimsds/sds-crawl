"""Source ingestion: sitemap URL, uploaded URL list, or website root URL.

Builds the `urls` table and hreflang groups. Only records URLs — crawling is
a separate phase.
"""
from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .config import settings
from .db import add_source, set_hreflang_group, upsert_url
from .util import DSU, host_of, parse_locale_section, registrable_domain


def _client() -> httpx.Client:
    c = settings()["crawl"]
    return httpx.Client(
        follow_redirects=True,
        timeout=c["request_timeout_s"],
        headers={"user-agent": c["user_agent"]},
    )


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


# ---- source kind detection ----------------------------------------------

def detect_kind(ref: str) -> str:
    p = Path(ref)
    if p.exists() and p.suffix.lower() in (".txt", ".csv"):
        return "urllist"
    low = ref.lower()
    if low.endswith(".xml") or "sitemap" in low:
        return "sitemap"
    if low.startswith("http://") or low.startswith("https://"):
        return "root"
    raise ValueError(f"Cannot determine source kind for: {ref}")


# ---- sitemap parsing ------------------------------------------------------

def _parse_sitemap_xml(content: bytes) -> tuple[list[str], list[dict]]:
    """Return (child_sitemap_urls, url_entries). One list is populated."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return [], []
    tag = _localname(root.tag)
    children_sitemaps: list[str] = []
    entries: list[dict] = []
    if tag == "sitemapindex":
        for sm in root:
            for child in sm:
                if _localname(child.tag) == "loc" and child.text:
                    children_sitemaps.append(child.text.strip())
        return children_sitemaps, entries
    # urlset (or unknown but with <url> children)
    for url_el in root:
        if _localname(url_el.tag) != "url":
            continue
        loc = None
        lastmod = None
        alternates: list[str] = []
        for child in url_el:
            name = _localname(child.tag)
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
            elif name == "link":
                rel = (child.attrib.get("rel") or "").lower()
                href = child.attrib.get("href")
                if rel == "alternate" and href:
                    alternates.append(href.strip())
        if loc:
            entries.append({"loc": loc, "lastmod": lastmod, "alternates": alternates})
    return children_sitemaps, entries


def _collect_sitemap_entries(client, entry_urls, seen=None) -> list[dict]:
    if seen is None:
        seen = set()
    out: list[dict] = []
    for url in entry_urls:
        if url in seen:
            continue
        seen.add(url)
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! failed to fetch sitemap {url}: {exc}")
            continue
        children, entries = _parse_sitemap_xml(resp.content)
        if children:
            print(f"  {url} -> sitemap index with {len(children)} children")
            out.extend(_collect_sitemap_entries(client, children, seen))
        else:
            out.extend(entries)
    return out


# ---- hreflang grouping + storage -----------------------------------------

def _store_entries(conn, source_id: int, entries: list[dict]) -> dict:
    dsu = DSU()
    all_urls: set[str] = set()
    external = 0
    primary = settings()["crawl"]["primary_domain"]

    for e in entries:
        loc = e["loc"]
        all_urls.add(loc)
        dsu.find(loc)
        for alt in e["alternates"]:
            all_urls.add(alt)
            dsu.union(loc, alt)
            if host_of(alt) not in (primary, f"www.{primary}") and registrable_domain(
                alt
            ) != primary:
                external += 1

    # Assign integer group ids to groups with >1 member.
    group_id_of: dict[str, int] = {}
    next_gid = 1
    for _root, members in dsu.groups().items():
        if len(members) > 1:
            for m in members:
                group_id_of[m] = next_gid
            next_gid += 1

    lastmod_of = {e["loc"]: e["lastmod"] for e in entries}
    on_domain = 0
    for url in sorted(all_urls):
        locale, section = parse_locale_section(url)
        uid = upsert_url(conn, source_id, url, locale, section, lastmod_of.get(url))
        gid = group_id_of.get(url)
        if gid is not None:
            set_hreflang_group(conn, uid, gid)
        if registrable_domain(url) == primary:
            on_domain += 1
    conn.commit()

    return {
        "total_urls": len(all_urls),
        "on_domain": on_domain,
        "external_recorded": external,
        "hreflang_groups": next_gid - 1,
    }


# ---- URL-list -------------------------------------------------------------

def _read_url_list(path: str) -> list[str]:
    urls: list[str] = []
    p = Path(path)
    if p.suffix.lower() == ".csv":
        with open(p, newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if not row:
                    continue
                cell = row[0].strip()
                if cell.lower().startswith("http"):
                    urls.append(cell)
    else:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line.lower().startswith("http"):
                urls.append(line)
    return urls


def _store_url_list(conn, source_id: int, urls: list[str]) -> dict:
    for url in urls:
        locale, section = parse_locale_section(url)
        upsert_url(conn, source_id, url, locale, section, None)
    conn.commit()
    return {"total_urls": len(urls), "on_domain": len(urls), "external_recorded": 0, "hreflang_groups": 0}


# ---- root URL discovery ---------------------------------------------------

_SITEMAP_RE = re.compile(r"^sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
COMMON_SITEMAP_PATHS = ["/sitemap.xml", "/sitemap/sitemap.xml", "/sitemap/sitemap-0.xml", "/sitemap_index.xml"]


def _discover_sitemaps(client, root: str) -> list[str]:
    found: list[str] = []
    try:
        robots = client.get(urljoin(root, "/robots.txt"))
        if robots.status_code < 400:
            found.extend(m.group(1).strip() for m in _SITEMAP_RE.finditer(robots.text))
    except Exception:  # noqa: BLE001
        pass
    if found:
        print(f"  robots.txt lists {len(found)} sitemap(s)")
        return found
    for path in COMMON_SITEMAP_PATHS:
        candidate = urljoin(root, path)
        try:
            r = client.get(candidate)
            if r.status_code < 400 and b"<" in r.content[:200] and (
                b"urlset" in r.content or b"sitemapindex" in r.content
            ):
                print(f"  discovered sitemap at {candidate}")
                return [candidate]
        except Exception:  # noqa: BLE001
            continue
    return []


def _link_crawl(client, root: str, cap: int = 500) -> list[str]:
    """Fallback: bounded same-domain BFS when no sitemap exists."""
    from selectolax.parser import HTMLParser

    domain = registrable_domain(root)
    seen: set[str] = set()
    queue = [root]
    found: list[str] = []
    while queue and len(found) < cap:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            r = client.get(url)
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
                continue
        except Exception:  # noqa: BLE001
            continue
        found.append(url)
        tree = HTMLParser(r.text)
        for a in tree.css("a[href]"):
            href = a.attributes.get("href")
            if not href:
                continue
            absu = urljoin(url, href).split("#")[0]
            if registrable_domain(absu) == domain and absu not in seen:
                queue.append(absu)
    print(f"  link-crawl fallback discovered {len(found)} URLs")
    return found


# ---- public API -----------------------------------------------------------

def add_and_ingest(conn, ref: str, name: str | None = None) -> dict:
    kind = detect_kind(ref)
    location = str(Path(ref).resolve()) if kind == "urllist" else ref
    domain = None if kind == "urllist" else registrable_domain(ref)
    source_id = add_source(conn, kind, location, name, domain)

    print(f"Source #{source_id} ({kind}): {location}")
    with _client() as client:
        if kind == "sitemap":
            entries = _collect_sitemap_entries(client, [location])
            print(f"  {len(entries)} <url> entries")
            stats = _store_entries(conn, source_id, entries)
        elif kind == "urllist":
            urls = _read_url_list(location)
            stats = _store_url_list(conn, source_id, urls)
        else:  # root
            sitemaps = _discover_sitemaps(client, location)
            if sitemaps:
                entries = _collect_sitemap_entries(client, sitemaps)
                print(f"  {len(entries)} <url> entries from discovered sitemap(s)")
                stats = _store_entries(conn, source_id, entries)
            else:
                urls = _link_crawl(client, location)
                stats = _store_url_list(conn, source_id, urls)

    stats["source_id"] = source_id
    print(
        f"Ingested: {stats['on_domain']} on-domain URLs, "
        f"{stats['hreflang_groups']} hreflang groups, "
        f"{stats['external_recorded']} external alternates recorded"
    )
    return stats
