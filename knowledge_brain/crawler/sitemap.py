"""Find every page URL in one or more sitemaps (Task 2.2).

A sitemap is a <urlset> that lists pages, or a <sitemapindex> that lists more sitemaps.
discover() follows indexes down to MAX_DEPTH levels, never reads a sitemap twice (so an index
that lists itself can't loop), and keeps each sitemap's URLs in file order, so the plan can show
its first and last URL. A sitemap that can't be read comes back with its error; the others carry
on. Works for any site: nothing here knows about one help center.
"""

import logging
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NamedTuple
from xml.etree import ElementTree

import httpx

from knowledge_brain.crawler.http import MAX_BODY_BYTES, BodyTooLarge, read_limited
from knowledge_brain.crawler.ratelimit import HostRateLimiter
from knowledge_brain.crawler.robots import RobotsRules

logger = logging.getLogger(__name__)

MAX_DEPTH = 3  # the sitemap the user added, its child sitemaps, and their children
MAX_SITEMAPS = 500  # per discovery, so a runaway index can't keep the crawler busy for hours


class SitemapError(Exception):
    """This sitemap couldn't be read. The message says why, for the plan."""


@dataclass
class PageEntry:
    url: str
    lastmod: datetime | None


class ParsedSitemap(NamedTuple):
    is_index: bool
    pages: list[PageEntry]  # a <urlset>'s entries, in file order, duplicates kept
    children: list[str]  # a <sitemapindex>'s sitemap URLs


@dataclass
class FoundSitemap:
    url: str
    parent_url: str | None
    depth: int = 0
    is_index: bool = False
    pages: list[PageEntry] = field(default_factory=list[PageEntry])
    children: list[str] = field(default_factory=list[str])
    error: str | None = None


def parse_sitemap(data: bytes) -> ParsedSitemap:
    """Parse sitemap XML, gzipped or not, with or without the sitemap namespace.

    Python's XML parser refuses entity-expansion bombs and never loads external entities, so
    parsing a stranger's file is safe.
    """
    if data[:2] == b"\x1f\x8b":  # the gzip signature, as in sitemap.xml.gz
        data = gunzip_limited(data)
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise SitemapError(f"Not valid XML: {error}") from error
    kind = _local_name(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError(f"Not a sitemap: the top element is <{kind}>")
    pages: list[PageEntry] = []
    children: list[str] = []
    for entry in root:
        fields = {_local_name(child.tag): (child.text or "").strip() for child in entry}
        loc = fields.get("loc", "")
        if not loc.startswith(("http://", "https://")):
            continue
        if kind == "urlset":
            pages.append(PageEntry(loc, _parse_lastmod(fields.get("lastmod"))))
        else:
            children.append(loc)
    return ParsedSitemap(kind == "sitemapindex", pages, children)


def gunzip_limited(data: bytes, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Unzip, refusing to expand past max_bytes: a tiny .gz file can unzip to gigabytes."""
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        unzipped = decompressor.decompress(data, max_bytes + 1)
    except zlib.error as error:
        raise SitemapError(f"Broken gzip data: {error}") from error
    if len(unzipped) > max_bytes:
        raise SitemapError(f"It unzips to more than {max_bytes:,} bytes")
    return unzipped


async def discover(
    urls: list[str], client: httpx.AsyncClient, robots: RobotsRules, limiter: HostRateLimiter
) -> list[FoundSitemap]:
    """Read the given sitemaps and every sitemap they list, breadth first, one at a time."""
    found: list[FoundSitemap] = []
    seen: set[str] = set()
    queue: deque[tuple[str, str | None, int]] = deque((url, None, 0) for url in urls)
    while queue:
        if len(found) >= MAX_SITEMAPS:
            logger.warning("Stopped after %d sitemaps; %d not read", MAX_SITEMAPS, len(queue))
            break
        url, parent_url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        sitemap = FoundSitemap(url, parent_url, depth)
        found.append(sitemap)
        try:
            parsed = await _read(url, client, robots, limiter)
        except SitemapError as error:
            sitemap.error = str(error)
            continue
        sitemap.is_index, sitemap.pages, sitemap.children = parsed
        if sitemap.is_index and depth + 1 >= MAX_DEPTH:
            sitemap.error = f"Nested more than {MAX_DEPTH} levels deep; its sitemaps weren't read"
        elif sitemap.is_index:
            queue.extend((child, url, depth + 1) for child in parsed.children)
    return found


async def _read(
    url: str, client: httpx.AsyncClient, robots: RobotsRules, limiter: HostRateLimiter
) -> ParsedSitemap:
    if not await robots.allowed(url):
        raise SitemapError("robots.txt doesn't allow reading this sitemap")
    await limiter.wait(url)
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                raise SitemapError(f"The server answered HTTP {response.status_code}")
            data = await read_limited(response)
    except httpx.HTTPError as error:
        raise SitemapError(f"Couldn't fetch it: {error}") from error
    except BodyTooLarge as error:
        raise SitemapError(str(error)) from error
    return parse_sitemap(data)


def _local_name(tag: str) -> str:
    """'{http://www.sitemaps.org/schemas/sitemap/0.9}loc' -> 'loc'."""
    return tag.rsplit("}", 1)[-1]


def _parse_lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
