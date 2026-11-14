"""Demo data, made once when the app first starts (the `seed` service): no model is called.

- Two businesses with their owners, to show that no business sees another's data:
  "Solution Center Demo" (owner@demo.local) and "Acme Store" (owner@acme.local), password from
  DEMO_PASSWORD (demo1234 by default). Demo logins are development only.
- The Solution Center Demo gets the v1 snapshot of the Brilliant Directories Solution Center:
  1,700 URLs (1,541 articles), 6,443 screenshot descriptions and their Voyage embeddings, all
  produced and paid for during v1. The chunks are rebuilt with today's chunker and each keeps
  its v1 embedding when the text is identical, so search by meaning works with no new spend.
- A few demo instructions and skills (the business rules a real owner would write), and the
  benchmark files (benchmark/v1) loaded into the Eval Lab.

    docker compose run --rm seed            # or: python -m scripts.seed
"""

import asyncio
import base64
import gzip
import json
import struct
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import insert, select

from core.config import REPO_ROOT, get_settings
from core.db import dispose_engines, owner_session, tenant_session
from core.models import User
from knowledge_brain.knowledge import create_skill, save_instruction
from knowledge_brain.models import (
    Chunk,
    Image,
    Page,
    PageSitemap,
    PageVersion,
    Sitemap,
    Source,
)
from knowledge_brain.search.chunk import chunk_page, image_chunk, place
from knowledge_brain.search.index import update_terms
from workspace.service import create_business_for_owner

SNAPSHOT = REPO_ROOT / "knowledge_brain" / "seed" / "brain-seed.jsonl.gz"
DEMO_SKILLS = REPO_ROOT / "scripts" / "demo_skills"


def _vector(encoded: str) -> list[float]:
    """v1 stored each embedding as base64 float16 bytes (2 bytes x 1,024)."""
    raw = base64.b64decode(encoded)
    return list(struct.unpack(f"<{len(raw) // 2}e", raw))


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def read_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as file:
        for line in file:
            record = json.loads(line)
            if "table" in record:
                tables.setdefault(record["table"], []).append(record["row"])
    return tables


async def load_snapshot(business_id: uuid.UUID) -> dict[str, int]:
    tables = read_snapshot(SNAPSHOT)
    sitemap_row = tables["sitemaps"][0]
    vectors = {row["content"]: row["embedding"] for row in tables["chunks"] if row.get("embedding")}
    async with tenant_session(business_id, role="brain") as session:
        source = Source(
            business_id=business_id,
            kind="sitemap",
            label=sitemap_row["url"],
            url=sitemap_row["url"],
            status="ready",
            recheck_days=1,
            miss_threshold=2,
            action_rule="quarantine_then_archive",
            next_check_at=datetime.now(UTC) + timedelta(days=1),
            last_synced_at=_when(sitemap_row["last_checked_at"]),
            settings={"rate": 1.0, "seeded_from": "v1 snapshot, 31 Oct 2026"},
        )
        session.add(source)
        await session.flush()
        sitemap = Sitemap(
            business_id=business_id,
            source_id=source.id,
            url=sitemap_row["url"],
            url_count=sitemap_row["entry_count"],
            last_checked_at=_when(sitemap_row["last_checked_at"]),
        )
        session.add(sitemap)
        await session.flush()
        page_ids: dict[int, uuid.UUID] = {}
        pages: list[dict[str, Any]] = []
        for row in tables["pages"]:
            if row["kind"] not in ("article", "listing"):
                continue
            page_id = uuid.uuid4()
            page_ids[row["id"]] = page_id
            breadcrumb = (
                " > ".join(part for part in (row.get("category"), row.get("folder")) if part)
                or None
            )
            pages.append(
                {
                    "id": page_id,
                    "business_id": business_id,
                    "source_id": source.id,
                    "url": row["url"],
                    "origin": "sitemap",
                    "kind": row["kind"],
                    "title": row.get("title"),
                    "breadcrumb": breadcrumb,
                    "content_md": row.get("content_md"),
                    "content_hash": row.get("content_hash"),
                    "embedded_hash": row.get("content_hash"),
                    "level": "active",
                    "http_status": row.get("http_status"),
                    "etag": row.get("etag"),
                    "last_modified": row.get("last_modified"),
                    "sitemap_lastmod": _when(row.get("sitemap_lastmod")),
                    "fetch_state": "fetched",
                    "fetched_at": _when(row.get("fetched_at")),
                    "changed_at": _when(row.get("fetched_at")),
                }
            )
        for start in range(0, len(pages), 500):
            await session.execute(insert(Page), pages[start : start + 500])
        links = [
            {
                "business_id": business_id,
                "page_id": page_ids[row["page_id"]],
                "sitemap_id": sitemap.id,
                "position": row["position"],
            }
            for row in tables["page_sitemaps"]
            if row["page_id"] in page_ids
        ]
        await session.execute(insert(PageSitemap), links)
        versions = [
            {
                "business_id": business_id,
                "page_id": p["id"],
                "version": 1,
                "change": "new",
                "content_hash": p["content_hash"] or "",
                "title": p["title"],
                "content_md": p["content_md"] or "",
            }
            for p in pages
            if p["kind"] == "article" and p["content_md"]
        ]
        for start in range(0, len(versions), 500):
            await session.execute(insert(PageVersion), versions[start : start + 500])
        image_rows: list[dict[str, Any]] = []
        images_by_page: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for row in tables["images"]:
            if row["page_id"] not in page_ids:
                continue
            item: dict[str, Any] = {
                "id": uuid.uuid4(),
                "business_id": business_id,
                "page_id": page_ids[row["page_id"]],
                "position": row["position"],
                "src_url": row["url"],
                "alt": row.get("alt"),
                "context": row.get("context"),
                "sha256": row.get("sha256"),
                "description": row.get("description"),
                "described_by": "claude-haiku-5-5" if row.get("description") else None,
                "state": "described" if row.get("description") else "pending",
            }
            image_rows.append(item)
            images_by_page.setdefault(item["page_id"], []).append(item)
        for start in range(0, len(image_rows), 1000):
            await session.execute(insert(Image), image_rows[start : start + 1000])
        chunks: list[dict[str, Any]] = []
        matched = 0
        for page in pages:
            if page["kind"] != "article" or not page["content_md"]:
                continue
            where = place(page["breadcrumb"], page["title"])
            built = [(chunk, None) for chunk in chunk_page(page["content_md"], where)]
            for image in sorted(images_by_page.get(page["id"], []), key=lambda i: i["position"]):
                if image["description"] and image["description"].strip().lower() != "decorative":
                    built.append(
                        (
                            image_chunk(
                                where, image["position"], image["alt"], image["description"]
                            ),
                            image["id"],
                        )
                    )
            for position, (chunk, image_id) in enumerate(built):
                vector = vectors.get(chunk.content)
                matched += vector is not None
                chunks.append(
                    {
                        "business_id": business_id,
                        "knowledge_type": "sitemap",
                        "page_id": page["id"],
                        "image_id": image_id,
                        "position": position,
                        "heading": chunk.heading[:500],
                        "anchor": chunk.anchor,
                        "content": chunk.content,
                        "token_count": chunk.token_count,
                        "searchable": True,
                        "embedding": _vector(vector) if vector else None,
                        "embedding_model": "voyage-4-lite" if vector else None,
                    }
                )
        ids: list[int] = []
        for start in range(0, len(chunks), 500):
            result = await session.execute(
                insert(Chunk).returning(Chunk.id), chunks[start : start + 500]
            )
            ids.extend(result.scalars().all())
        for start in range(0, len(ids), 2000):
            await update_terms(session, business_id, ids[start : start + 2000])
    return {
        "pages": len(pages),
        "images": len(image_rows),
        "chunks": len(chunks),
        "chunks_with_v1_vectors": matched,
    }


DEMO_INSTRUCTIONS = [
    (
        "Refunds and billing disputes",
        "Never promise a refund, a credit or a discount. Say that the Billing team will reply within one business day, and hand the conversation to a human.",
    ),
    (
        "Support hours",
        "Our support team answers emails Monday to Friday, 9 am to 6 pm US Eastern time. There is no live chat at weekends; weekend emails are answered on Monday.",
    ),
    (
        "Passwords and account access",
        "Never ask for, accept or repeat a password. For login problems, give the password reset steps from the knowledge and offer to send the reset link from the admin area.",
    ),
    (
        "Custom development requests",
        "When a client asks for a feature the product doesn't have, say so honestly, then offer a free 15-minute scoping call with the solutions team. Never quote a price for custom work.",
    ),
]


async def seed_demo_knowledge(business_id: uuid.UUID) -> None:
    async with tenant_session(business_id, role="brain") as session:
        for title, body in DEMO_INSTRUCTIONS:
            await save_instruction(
                session,
                business_id,
                instruction_id=None,
                title=title,
                body=body,
                enabled=True,
                all_roles=True,
                role_ids=[],
                user_id=None,
            )
        for path in sorted(DEMO_SKILLS.glob("*.md")):
            await create_skill(
                session,
                business_id,
                path.read_text(encoding="utf-8"),
                author="default",
                status="live",
                user_id=None,
                note="Demo skill from the seed",
            )


async def seed_acme(business_id: uuid.UUID) -> None:
    async with tenant_session(business_id, role="brain") as session:
        await save_instruction(
            session,
            business_id,
            instruction_id=None,
            title="Shipping",
            body="Acme orders ship within 2 business days. Express shipping is not offered.",
            enabled=True,
            all_roles=True,
            role_ids=[],
            user_id=None,
        )


async def main() -> None:
    settings = get_settings()
    async with owner_session() as session:
        if await session.scalar(select(User.id).where(User.email == "owner@demo.local")):
            print("Already seeded; nothing to do.")
            return
        password = settings.demo_password.get_secret_value()
        demo = await create_business_for_owner(
            session,
            owner_email="owner@demo.local",
            owner_name="Demo Owner",
            password=password,
            business_name="Solution Center Demo",
        )
        acme = await create_business_for_owner(
            session,
            owner_email="owner@acme.local",
            owner_name="Acme Owner",
            password=password,
            business_name="Acme Store",
        )
    if SNAPSHOT.exists():
        counts = await load_snapshot(demo)
        print(f"Solution Center snapshot loaded: {counts}")
    await seed_demo_knowledge(demo)
    await seed_acme(acme)
    try:
        from eval_lab.benchmark import load_benchmarks

        loaded = await load_benchmarks(demo)
        print(f"Benchmarks loaded: {loaded}")
    except ImportError:
        pass
    print("Seeded: owner@demo.local and owner@acme.local (password from DEMO_PASSWORD).")
    await dispose_engines()


if __name__ == "__main__":
    asyncio.run(main())
