"""Uploaded documents (Go Live, Task 6.1): PDF, Word, Markdown and text become pages.

The file is kept on disk (STACK.md S4); its text is stored as a page with origin "document",
so it is chunked, embedded, cited and versioned like any web page.
"""

import hashlib
import io
import re
import uuid

import docx
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from core import storage
from knowledge_brain.crawler.parse import content_hash
from knowledge_brain.models import Document, Page, Source
from knowledge_brain.search.index import index_page

MAX_BYTES = 20 * 1024 * 1024
TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
}


class DocumentError(ValueError):
    pass


def extract_text(filename: str, data: bytes) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in TYPES:
        raise DocumentError("Upload a PDF, DOCX, Markdown or text file.")
    if len(data) > MAX_BYTES:
        raise DocumentError("The file is larger than 20 MB.")
    if ext == "pdf":
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(
            f"## Page {n}\n\n{text}" for n, text in enumerate(pages, start=1) if text
        )
    if ext == "docx":
        document = docx.Document(io.BytesIO(data))
        lines: list[str] = []
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style = (
                paragraph.style.name if paragraph.style is not None and paragraph.style.name else ""
            )
            level = re.search(r"Heading (\d)", style)
            lines.append(f"{'#' * min(int(level.group(1)) + 1, 4)} {text}" if level else text)
        return "\n\n".join(lines)
    return data.decode("utf-8", errors="replace")


async def add_document(
    session: AsyncSession,
    business_id: uuid.UUID,
    filename: str,
    data: bytes,
    *,
    user_id: uuid.UUID | None,
) -> Page:
    text = extract_text(filename, data).strip()
    if not text:
        raise DocumentError("No text could be read from this file (a scanned PDF needs OCR).")
    sha = hashlib.sha256(data).hexdigest()
    source = Source(
        business_id=business_id, kind="document", label=filename, status="ready", created_by=user_id
    )
    session.add(source)
    await session.flush()
    document_id = uuid.uuid4()
    path = storage.save(business_id, "documents", f"{document_id}-{filename}", data)
    title = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    page = Page(
        business_id=business_id,
        source_id=source.id,
        url=f"doc://{document_id}/{filename}",
        origin="document",
        kind="article",
        title=title,
        content_md=text,
        fetch_state="fetched",
        content_hash=content_hash(title, text, []),
    )
    session.add(page)
    await session.flush()
    session.add(
        Document(
            id=document_id,
            business_id=business_id,
            source_id=source.id,
            page_id=page.id,
            filename=filename,
            mime=TYPES[filename.lower().rsplit(".", 1)[-1]],
            size_bytes=len(data),
            sha256=sha,
            storage_path=path,
            uploaded_by=user_id,
        )
    )
    await index_page(session, page)
    return page
