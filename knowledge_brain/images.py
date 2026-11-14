"""Understand the images (Task 2.3): each screenshot gets a description of what it shows.

The model is the business's choice for "Read a screenshot" (Settings > Models); the Eval Lab
compares Haiku, Gemini 3.8 Flash, Gemini 3 Flash, Nemotron Super VL and gemma4:e4b on it. A
screenshot is described once per business: the same bytes (SHA-256) reuse the description.
The image is kept on disk so the source viewer can show it. Descriptions are written outside
any database transaction, so a slow model never holds a lock.
"""

import hashlib
import mimetypes
import uuid

from sqlalchemy import select

from core import storage
from core.db import tenant_session
from core.llm.client import LlmClient
from core.llm.types import ChatTurn, ImagePart, LlmError, LlmRequest, MissingConnection
from core.prompts import live_prompt
from knowledge_brain.models import Image, Page
from knowledge_brain.reading import Crawler
from knowledge_brain.search.index import index_page

MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


async def describe_pending(
    business_id: uuid.UUID, c: Crawler, *, limit: int = 20
) -> tuple[int, str | None]:
    """Describe up to `limit` waiting screenshots. Returns (described, why it stopped early)."""
    async with tenant_session(business_id, role="brain") as session:
        waiting = list(
            (
                await session.execute(
                    select(
                        Image.id, Image.src_url, Image.alt, Image.context, Image.page_id, Page.title
                    )
                    .join(Page, Page.id == Image.page_id)
                    .where(Image.state == "pending")
                    .order_by(Image.created_at)
                    .limit(limit)
                )
            ).all()
        )
        prompt = await live_prompt(session, "brain.describe_screenshot")
    llm = LlmClient(business_id, role="brain")
    described = 0
    touched_pages: set[uuid.UUID] = set()
    for row in waiting:
        data, media_type = await _download(c, row.src_url)
        if data is None:
            await _mark(business_id, row.id, state="skipped", error=media_type)
            continue
        sha = hashlib.sha256(data).hexdigest()
        async with tenant_session(business_id, role="brain") as session:
            known = (
                await session.execute(
                    select(Image.description, Image.described_by)
                    .where(Image.sha256 == sha, Image.state == "described")
                    .limit(1)
                )
            ).first()
        description, model_key = (known.description, known.described_by) if known else (None, None)
        if description is None:
            try:
                result = await llm.call(
                    LlmRequest(
                        business_id=business_id,
                        step="brain.read_screenshot",
                        job="read_screenshot",
                        system=[prompt.body],
                        messages=[
                            ChatTurn(
                                "user",
                                f"Page: {row.title or ''}\nAlt text: {row.alt or '-'}\n"
                                f"Context: {row.context or '-'}",
                                (ImagePart(data, media_type),),
                            )
                        ],
                        max_tokens=1500,
                        effort="low",
                        prompt_key=prompt.key,
                        prompt_version_id=prompt.version_id,
                    )
                )
            except MissingConnection as exc:
                return described, str(exc)  # no key yet: the screenshots wait, nothing fails
            except LlmError as exc:
                await _mark(business_id, row.id, state="failed", error=str(exc)[:300])
                continue
            description, model_key = result.text.strip(), result.model_key
        path = storage.save(
            business_id, "images", f"{sha}{mimetypes.guess_extension(media_type) or '.img'}", data
        )
        await _mark(
            business_id,
            row.id,
            state="described",
            description=description,
            described_by=model_key,
            sha256=sha,
            storage_path=path,
        )
        touched_pages.add(row.page_id)
        described += 1
    async with tenant_session(business_id, role="brain") as session:
        for page_id in touched_pages:
            page = await session.get(Page, page_id)
            if page is not None:
                await index_page(session, page)  # the new descriptions become searchable chunks
    return described, None


async def _download(c: Crawler, url: str) -> tuple[bytes | None, str]:
    try:
        await c.limiter.wait(url)
        response = await c.client.get(url, follow_redirects=True)
    except Exception as exc:
        return None, f"download failed: {exc}"[:200]
    media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"
    if media_type not in MEDIA_TYPES:
        return None, f"not a supported image ({media_type or 'unknown type'})"
    if len(response.content) > MAX_IMAGE_BYTES:
        return None, "image larger than 5 MB"
    return response.content, media_type


async def _mark(business_id: uuid.UUID, image_id: uuid.UUID, **values: object) -> None:
    async with tenant_session(business_id, role="brain") as session:
        image = await session.get(Image, image_id)
        if image is not None:
            for key, value in values.items():
                setattr(image, key, value)
