"""Turn any web page into clean Markdown, its place on the site, and its images.

Any sitemap must work (Feature 2), so nothing here is tied to one help center:
1. Navigation, headers, footers, sidebars, cookie banners, forms and scripts are removed.
2. The main content is the first well-known content container with real text (a help-center
   article body, <article>, <main>, a docs or blog post body); failing that, the block with the
   most text that isn't links (a small readability-style score); failing that, the <body>.
3. The title is the main <h1>, else og:title, else the <title> before " | " or " - ".
4. The breadcrumb, when the site has one, gives the page's place: "Emails > Email Accounts".
5. A page that is mostly links is a "listing" (a category or folder page): tracked, not embedded.
6. A page with almost no text but a JavaScript app shell is flagged "needs a browser" (STACK S3).
7. Each image becomes "[Screenshot N: alt]" in the Markdown, plus an ImageRef with its address,
   alt text and the text before it, which the screenshot reader gets as context.

The content hash covers the title, the words and the image addresses: it changes when the page
really changes, not when only whitespace or an image's cache-busting ?query does.
"""

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from markdownify import markdownify  # type: ignore[import-untyped]

CONTENT_SELECTORS = (
    "#article-body",  # Freshdesk
    ".article-body",  # Zendesk
    "article .entry-content",
    ".post-content",
    ".entry-content",
    ".markdown-body",  # docs sites
    "article",
    "[role=main]",
    "main",
    "#content",
    ".content",
)
REMOVE = (
    "script",
    "style",
    "noscript",
    "svg",
    "form",
    "button",
    "iframe",
    "[aria-hidden=true]",
    "p.the-article-link",  # Freshdesk's permalink line
)
# Page chrome, removed unless it holds the page's <h1> (a blog's <header> often does)
CHROME = (
    "nav",
    "header",
    "footer",
    "aside",
    "[role=navigation]",
    "[role=banner]",
    "[role=contentinfo]",
)
NOISE_WORDS = (
    "cookie",
    "consent",
    "sidebar",
    "related",
    "share",
    "social",
    "comment",
    "advert",
    "newsletter",
    "subscribe",
    "menu",
    "navbar",
    "breadcrumb",
    "pagination",
    "footer",
)
BREADCRUMBS = (
    ".breadcrumb",
    ".breadcrumbs",
    "nav[aria-label*=readcrumb]",
    "[itemtype*=BreadcrumbList]",
)
TEXT_BLOCKS = ["p", "li", "h2", "h3", "h4", "td"]
MIN_CONTENT_CHARS = 200
MAX_CONTEXT_CHARS = 500
APP_SHELL = re.compile(r'id="(root|app|__next)"|__NEXT_DATA__|ng-app|data-reactroot', re.IGNORECASE)


@dataclass
class ImageRef:
    position: int
    url: str
    alt: str | None
    context: str | None


@dataclass
class ParsedPage:
    title: str
    breadcrumb: str | None
    markdown: str
    images: list[ImageRef]
    content_hash: str
    is_article: bool  # False for a page that is mostly links
    needs_browser: bool  # True when the text is built by JavaScript in the browser


def parse_page(html: bytes | str, url: str) -> ParsedPage:
    raw = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    soup = BeautifulSoup(raw, "html.parser")
    breadcrumb = _breadcrumb(soup)
    og_title = soup.find("meta", attrs={"property": "og:title"})
    page_title = soup.title.get_text(strip=True) if soup.title else ""
    for selector in REMOVE:
        for element in soup.select(selector):
            element.decompose()
    for selector in CHROME:
        for element in soup.select(selector):
            if not element.decomposed and element.find("h1") is None:
                element.decompose()
    _remove_noise(soup)
    main = _main_content(soup)
    title = _title(main, og_title, page_title)
    if main is None:
        empty_hash = content_hash(title, "", [])
        return ParsedPage(title, breadcrumb, "", [], empty_hash, False, bool(APP_SHELL.search(raw)))
    if heading := main.find("h1"):
        heading.decompose()  # the title is kept on its own
    for video in main.select("video"):
        video.replace_with(NavigableString("[Video]"))
    images = _swap_images_for_markers(main, url)
    markdown = _to_markdown(main)
    text_chars = len(re.sub(r"\[Screenshot \d+[^\]]*\]", "", markdown).strip())
    is_article = text_chars >= 120 and not _mostly_links(main)
    needs_browser = text_chars < MIN_CONTENT_CHARS and bool(APP_SHELL.search(raw))
    return ParsedPage(
        title,
        breadcrumb,
        markdown,
        images,
        content_hash(title, markdown, images),
        is_article,
        needs_browser,
    )


def content_hash(title: str, markdown: str, images: list[ImageRef]) -> str:
    words = " ".join(markdown.split())
    parts = [title, words, *(without_query(image.url) for image in images)]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def without_query(url: str) -> str:
    scheme, netloc, path, _, _ = urlsplit(url)
    return urlunsplit((scheme, netloc, path, "", ""))


def _remove_noise(soup: BeautifulSoup) -> None:
    for element in soup.find_all(True):
        if element.decomposed:
            continue  # already removed with a parent
        if element.name in ("html", "body", "main", "article"):
            continue
        attrs = element.attrs or {}
        names = " ".join([*(attrs.get("class") or []), str(attrs.get("id") or "")]).lower()
        if names and any(word in names for word in NOISE_WORDS):
            element.decompose()


def _main_content(soup: BeautifulSoup) -> Tag | None:
    for selector in CONTENT_SELECTORS:
        tag = soup.select_one(selector)
        if tag is not None and len(tag.get_text(" ", strip=True)) >= MIN_CONTENT_CHARS:
            return tag
    best: Tag | None = None
    best_score = 0.0
    for block in soup.find_all(["div", "section"]):
        text = len(block.get_text(" ", strip=True))
        links = sum(len(a.get_text(" ", strip=True)) for a in block.find_all("a"))
        paragraphs = len(block.find_all("p", recursive=False))
        score = (text - 2 * links) * (1 + 0.2 * paragraphs)
        if score > best_score:
            best, best_score = block, score
    if best is not None and best_score >= MIN_CONTENT_CHARS:
        return best
    return soup.body


def _mostly_links(main: Tag) -> bool:
    text = len(main.get_text(" ", strip=True))
    links = sum(len(a.get_text(" ", strip=True)) for a in main.find_all("a"))
    return text > 0 and links / text > 0.6 and text - links < 300


def _title(main: Tag | None, og_title: object, page_title: str) -> str:
    heading = main.find("h1") if main is not None else None
    if isinstance(heading, Tag) and (text := heading.get_text(" ", strip=True)):
        return text
    if isinstance(og_title, Tag) and isinstance(og_title.get("content"), str):
        return str(og_title.get("content")).strip()
    return re.split(r"\s[|\-\u2013]\s", page_title)[0].strip()  # " | ", " - " or an en dash


def _breadcrumb(soup: BeautifulSoup) -> str | None:
    for selector in BREADCRUMBS:
        container = soup.select_one(selector)
        if container is None:
            continue
        parts: list[str] = []
        for link in container.find_all(["a", "span", "li"]):
            text = link.get_text(" ", strip=True)
            text = re.sub(r"^[A-Za-z ]+:\s*", "", text).strip()  # "Section: Emails" -> "Emails"
            if (
                text
                and text.lower() not in ("home", "support home", "solution home")
                and text not in parts
            ):
                parts.append(text)
        if parts:
            return " > ".join(parts[:5])
    return None


def _swap_images_for_markers(body: Tag, page_url: str) -> list[ImageRef]:
    images: list[ImageRef] = []
    for img in body.select("img"):
        src = img.get("src") or img.get("data-src")
        width, height = str(img.get("width") or ""), str(img.get("height") or "")
        tiny = width in ("1", "0") or height in ("1", "0")
        if (
            not isinstance(src, str)
            or src.startswith("data:")
            or src.rstrip("/").endswith("/hit")
            or tiny
        ):
            img.decompose()  # tracking pixels, inline icons
            continue
        alt_value = img.get("alt")
        alt = alt_value.strip() if isinstance(alt_value, str) and alt_value.strip() else None
        position = len(images) + 1
        images.append(ImageRef(position, urljoin(page_url, src), alt, _context(img, body)))
        marker = f"[Screenshot {position}: {alt}]" if alt else f"[Screenshot {position}]"
        img.replace_with(NavigableString(marker))
    return images


def _context(img: Tag, body: Tag) -> str | None:
    for block in img.find_all_previous(TEXT_BLOCKS):
        if body not in block.parents:
            break
        if text := block.get_text(" ", strip=True):
            return text[:MAX_CONTEXT_CHARS]
    return None


def _to_markdown(body: Tag) -> str:
    converted: str = markdownify(
        str(body),
        heading_style="ATX",
        bullets="-",
        strip=["a"],  # keep link text, drop addresses: they only add noise to search
        escape_underscores=False,
        escape_asterisks=False,
    )
    lines = [line.rstrip() for line in converted.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
