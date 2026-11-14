"""Split a page into search chunks (DATABASE.md D6).

300 to 500 tokens (about 4 characters per token), split at headings first, then at paragraph
ends, never inside a paragraph. No overlap: every chunk starts with its place ("Emails > Email
Accounts > Add an account > Step 2"), which carries the context an overlap would. Each chunk
remembers which paragraphs it holds ("p-4-7"), so a citation opens those exact paragraphs.
"""

import re
from dataclasses import dataclass

CHARS_PER_TOKEN = 4
MIN_TOKENS, MAX_TOKENS = 300, 500
MIN_CHARS, MAX_CHARS = MIN_TOKENS * CHARS_PER_TOKEN, MAX_TOKENS * CHARS_PER_TOKEN


@dataclass
class ChunkText:
    heading: str
    content: str
    token_count: int
    anchor: str  # p-<first paragraph>-<last paragraph>


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def place(breadcrumb: str | None, title: str | None) -> str:
    return " > ".join(part for part in (breadcrumb, title) if part)


def blocks(markdown: str) -> list[str]:
    """The page's paragraphs, headings and lists, as the source viewer numbers them (p-0, p-1 ...).

    A paragraph too long for a chunk is cut at a line break or sentence end.
    """
    found: list[str] = []
    for paragraph in re.split(r"\n\s*\n", markdown or ""):
        rest = paragraph.strip()
        while len(rest) > MAX_CHARS:
            cut = _cut_point(rest)
            found.append(rest[:cut].strip())
            rest = rest[cut:].strip()
        if rest:
            found.append(rest)
    return found


def chunk_page(markdown: str, where: str) -> list[ChunkText]:
    """Pack the paragraphs into chunks. A heading starts a new chunk once the current one holds
    MIN_CHARS; a paragraph that would push a chunk past MAX_CHARS starts one too."""
    groups: list[tuple[str | None, list[tuple[int, str]]]] = []
    heading: str | None = None
    for index, block in enumerate(blocks(markdown)):
        is_heading = block.startswith("#")
        size = sum(len(text) for _, text in groups[-1][1]) if groups else 0
        if not groups or size + len(block) > MAX_CHARS or (is_heading and size >= MIN_CHARS):
            groups.append((heading, []))
        if is_heading:
            heading = block.lstrip("#").strip()
        groups[-1][1].append((index, block))
    chunks: list[ChunkText] = []
    for start_heading, members in groups:
        first_is_heading = members[0][1].startswith("#")
        label = f"{where} > {start_heading}" if start_heading and not first_is_heading else where
        body = "\n\n".join(text for _, text in members)
        content = f"{label}\n\n{body}"
        anchor = f"p-{members[0][0]}-{members[-1][0]}"
        chunks.append(ChunkText(label, content, estimate_tokens(content), anchor))
    return chunks


def image_chunk(where: str, position: int, alt: str | None, description: str) -> ChunkText:
    """One screenshot's description, named like its marker in the page text (D6)."""
    label = f"{where} > Screenshot {position}" + (f": {alt}" if alt else "")
    content = f"{label}\n\n{description}"
    return ChunkText(label, content, estimate_tokens(content), f"img-{position}")


def text_chunk(heading: str, body: str, anchor: str | None = None) -> ChunkText:
    """A whole short text as one chunk: an instruction, a skill, an email, a lesson."""
    content = f"{heading}\n\n{body}".strip()
    return ChunkText(heading, content, estimate_tokens(content), anchor or "all")


def _cut_point(text: str) -> int:
    window = text[:MAX_CHARS]
    for separator in ("\n", ". ", "; ", " "):
        position = window.rfind(separator)
        if position > MAX_CHARS // 2:
            return position + len(separator)
    return MAX_CHARS
