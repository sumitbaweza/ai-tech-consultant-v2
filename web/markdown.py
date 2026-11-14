"""A small, safe Markdown-to-HTML for answers, lessons and skills.

Everything is HTML-escaped first, then a few patterns are turned into tags: headings, lists,
bold, inline code, code blocks, links (http and https only) and [S3] citations. Model output
is untrusted, so nothing it writes can become a script or an attribute.
"""

import html
import re

from markupsafe import Markup

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_CITE = re.compile(r"\[(S\d+)\]")


def _inline(text: str, cite_url: str | None) -> str:
    text = _CODE.sub(r"<code>\1</code>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _LINK.sub(r'<a href="\2" rel="noopener noreferrer" target="_blank">\1</a>', text)
    if cite_url is not None:
        text = _CITE.sub(
            lambda m: f'<a class="cite" href="{cite_url}{m.group(1)}">{m.group(1)}</a>', text
        )
    return text


def render_markdown(source: str, *, cite_url: str | None = None) -> Markup:
    """cite_url: where a [S3] citation links to, e.g. "/chat/answers/<id>/source/"."""
    lines = html.escape(source or "", quote=True).splitlines()
    out: list[str] = []
    in_list: str | None = None
    in_code = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + _inline(" ".join(paragraph), cite_url) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    for line in lines:
        if line.strip().startswith("```"):
            flush_paragraph()
            close_list()
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(line + "\n")
            continue
        stripped = line.strip()
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if not stripped:
            flush_paragraph()
            close_list()
        elif heading:
            flush_paragraph()
            close_list()
            level = min(len(heading.group(1)) + 2, 6)
            out.append(f"<h{level}>{_inline(heading.group(2), cite_url)}</h{level}>")
        elif numbered or bullet:
            flush_paragraph()
            tag = "ol" if numbered else "ul"
            if in_list != tag:
                close_list()
                out.append(f"<{tag}>")
                in_list = tag
            item = (numbered or bullet).group(1)  # type: ignore[union-attr]
            out.append(f"<li>{_inline(item, cite_url)}</li>")
        else:
            close_list()
            paragraph.append(stripped)
    flush_paragraph()
    close_list()
    if in_code:
        out.append("</code></pre>")
    return Markup('<div class="md">' + "\n".join(out) + "</div>")
