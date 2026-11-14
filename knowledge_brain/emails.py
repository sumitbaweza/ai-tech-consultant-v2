"""Past emails make the skills better (Task 2.6, STACK.md S5, DATABASE.md D9).

1. Fetch: Gmail's API with gmail.readonly, threads in pages of 50, at most 5 fetched at once,
   the newest 200 per sync. A thread is fetched again only when its historyId changed.
   Message text is masked for personal data before it is stored; attachments are listed by
   name and size only.
2. Notes: a person writes a note on a whole thread or on one message.
3. Improve: Claude Opus 5.5 reads the notes with their threads and the current skills, and
   proposes skill changes (or new skills), saying what changed and which notes it used.
   Nothing is used until a human approves (knowledge.approve_proposal).
"""

import asyncio
import base64
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel
from sqlalchemy import select

from core import google
from core.crypto import decrypt
from core.db import tenant_session
from core.llm.client import LlmClient
from core.llm.providers.base import jdict, jlist
from core.llm.types import ChatTurn, LlmRequest
from core.models import Connection
from core.pii import mask
from core.prompts import live_prompt
from knowledge_brain.models import (
    EmailMessage,
    EmailNote,
    EmailThread,
    Skill,
    SkillProposal,
    SkillVersion,
)
from knowledge_brain.search.index import index_email_thread

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
PAGE_SIZE = 50
MAX_THREADS = 200
AT_ONCE = 5


class GmailNotConnected(RuntimeError):
    pass


async def _token(business_id: uuid.UUID) -> str:
    async with tenant_session(business_id, role="brain") as session:
        connection = (
            await session.execute(select(Connection).where(Connection.kind == "google"))
        ).scalar_one_or_none()
        if connection is None or connection.secret_enc is None:
            raise GmailNotConnected("Connect Gmail in Settings > Keys and connections first.")
        refresh = decrypt(connection.secret_enc, business_id=business_id)
    return await google.access_token(refresh)


def _body_text(payload: dict[str, Any]) -> str:
    """The plain text of a message: its text/plain part, or its HTML turned into text."""
    parts = [payload, *[jdict(p) for p in jlist(payload.get("parts"))]]
    nested = [jdict(sub) for p in parts for sub in jlist(p.get("parts"))]
    for part in [*parts, *nested]:
        data = jdict(part.get("body")).get("data")
        if part.get("mimeType") == "text/plain" and data:
            return base64.urlsafe_b64decode(str(data) + "==").decode("utf-8", "replace")
    for part in [*parts, *nested]:
        data = jdict(part.get("body")).get("data")
        if part.get("mimeType") == "text/html" and data:
            html = base64.urlsafe_b64decode(str(data) + "==").decode("utf-8", "replace")
            return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    return ""


def _attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for part in [jdict(p) for p in jlist(payload.get("parts"))]:
        if part.get("filename"):
            found.append(
                {
                    "name": part["filename"],
                    "mime": part.get("mimeType"),
                    "size": jdict(part.get("body")).get("size", 0),
                }
            )
    return found


async def sync_threads(
    business_id: uuid.UUID,
    *,
    max_threads: int = MAX_THREADS,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    token = await _token(business_id)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60, headers=headers, transport=transport) as http:
        listed: list[dict[str, Any]] = []
        page_token: str | None = None
        while len(listed) < max_threads:
            params: dict[str, Any] = {"maxResults": PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            data = jdict(
                (await http.get(f"{GMAIL}/threads", params=params)).raise_for_status().json()
            )
            listed.extend(jdict(t) for t in jlist(data.get("threads")))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        listed = listed[:max_threads]
        async with tenant_session(business_id, role="brain") as session:
            known = {
                row.gmail_thread_id: row.history_id
                for row in (
                    await session.execute(
                        select(EmailThread.gmail_thread_id, EmailThread.history_id)
                    )
                ).all()
            }
        todo = [t for t in listed if known.get(str(t.get("id"))) != str(t.get("historyId"))]
        gate = asyncio.Semaphore(AT_ONCE)

        async def fetch(thread_id: str) -> dict[str, Any]:
            async with gate:
                response = await http.get(f"{GMAIL}/threads/{thread_id}", params={"format": "full"})
                return jdict(response.raise_for_status().json())

        fetched = await asyncio.gather(*(fetch(str(t["id"])) for t in todo))
    for data in fetched:
        await store_thread(business_id, data)
    return {
        "listed": len(listed),
        "fetched": len(fetched),
        "skipped_unchanged": len(listed) - len(fetched),
    }


async def store_thread(business_id: uuid.UUID, data: dict[str, Any]) -> uuid.UUID:
    messages = [jdict(m) for m in jlist(data.get("messages"))]
    async with tenant_session(business_id, role="brain") as session:
        thread = (
            await session.execute(
                select(EmailThread).where(EmailThread.gmail_thread_id == str(data["id"]))
            )
        ).scalar_one_or_none()
        if thread is None:
            thread = EmailThread(
                business_id=business_id, gmail_thread_id=str(data["id"]), subject=""
            )
            session.add(thread)
            await session.flush()
        people: list[str] = []
        last_sent: datetime | None = None
        for position, message in enumerate(messages):
            payload = jdict(message.get("payload"))
            heads = {
                str(h.get("name", "")).lower(): str(h.get("value", ""))
                for h in map(jdict, jlist(payload.get("headers")))
            }
            sent = _date(heads.get("date"))
            last_sent = sent or last_sent
            sender = mask(heads.get("from", ""))
            people.append(sender)
            row = (
                await session.execute(
                    select(EmailMessage).where(
                        EmailMessage.gmail_message_id == str(message.get("id"))
                    )
                )
            ).scalar_one_or_none()
            row = row or EmailMessage(
                business_id=business_id,
                thread_id=thread.id,
                gmail_message_id=str(message.get("id")),
                position=position,
                from_addr="",
                subject="",
                body_text="",
            )
            row.position, row.from_addr, row.sent_at = position, sender, sent
            row.to_addrs = [mask(a.strip()) for a in heads.get("to", "").split(",") if a.strip()]
            row.subject = heads.get("subject", "")[:500]
            row.body_text = mask(_body_text(payload))[:20000]
            row.attachments = _attachments(payload)
            session.add(row)
            if position == 0:
                thread.subject = row.subject or "(no subject)"
        thread.participants = list(dict.fromkeys(people))[:10]
        thread.snippet = mask(str(data.get("snippet", "")))[:300] if data.get("snippet") else None
        thread.message_count, thread.last_message_at = len(messages), last_sent
        thread.history_id, thread.fetched_at = str(data.get("historyId", "")), datetime.now(UTC)
        await session.flush()
        await index_email_thread(session, thread)
        return thread.id


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None


class _Proposal(BaseModel):
    skill_key: str
    name: str
    markdown: str
    rationale: str
    changes: list[str]
    note_ids: list[str]


class _Proposals(BaseModel):
    proposals: list[_Proposal]


async def improve_from_notes(business_id: uuid.UUID, *, user_id: uuid.UUID | None) -> int:
    """Opus turns the new notes into skill proposals. Returns how many it proposed."""
    async with tenant_session(business_id, role="brain") as session:
        notes = (
            (await session.execute(select(EmailNote).where(EmailNote.status == "new")))
            .scalars()
            .all()
        )
        if not notes:
            return 0
        threads: dict[uuid.UUID, EmailThread] = {}
        lines: list[str] = []
        for note in notes:
            thread = threads.get(note.thread_id) or await session.get(EmailThread, note.thread_id)
            if thread is None:
                continue
            threads[thread.id] = thread
            target = "the whole thread"
            if note.message_id:
                message = await session.get(EmailMessage, note.message_id)
                target = (
                    f"message {message.position + 1}: {message.body_text[:1500]}"
                    if message
                    else target
                )
            lines.append(
                f'<note id="{note.id}" thread="{thread.subject}">On {target}\nNote: {note.body}</note>'
            )
        for thread in threads.values():
            messages = (
                (
                    await session.execute(
                        select(EmailMessage)
                        .where(EmailMessage.thread_id == thread.id)
                        .order_by(EmailMessage.position)
                    )
                )
                .scalars()
                .all()
            )
            text = "\n---\n".join(f"{m.from_addr}: {m.body_text[:1500]}" for m in messages)
            lines.append(f'<thread subject="{thread.subject}">\n{text}\n</thread>')
        skills = (
            await session.execute(
                select(Skill, SkillVersion)
                .join(SkillVersion, SkillVersion.skill_id == Skill.id)
                .where(SkillVersion.status == "live")
            )
        ).all()
        skill_text = (
            "\n\n".join(f'<skill key="{s.key}">\n{v.markdown}\n</skill>' for s, v in skills)
            or "(no skills yet)"
        )
        prompt = await live_prompt(session, "brain.improve_skill")
        by_key = {s.key: (s, v) for s, v in skills}
    result = await LlmClient(business_id, role="brain").call(
        LlmRequest(
            business_id=business_id,
            step="brain.improve_skill",
            job="improve_skill",
            system=[prompt.body],
            messages=[
                ChatTurn("user", f"<skills>\n{skill_text}\n</skills>\n\n" + "\n\n".join(lines))
            ],
            schema=_Proposals,
            max_tokens=16000,
            effort="high",
            prompt_key=prompt.key,
            prompt_version_id=prompt.version_id,
        )
    )
    assert isinstance(result.parsed, _Proposals)
    note_ids = {str(n.id): n.id for n in notes}
    count = 0
    async with tenant_session(business_id, role="brain") as session:
        for item in result.parsed.proposals:
            used = [note_ids[i] for i in item.note_ids if i in note_ids]
            existing = by_key.get(item.skill_key)
            proposal = SkillProposal(
                business_id=business_id,
                skill_id=existing[0].id if existing else None,
                origin="email_notes",
                base_version_id=existing[1].id if existing else None,
                proposed_name=item.name,
                proposed_markdown=item.markdown,
                rationale=item.rationale,
                changes=item.changes,
                note_ids=used,
                model_key=result.model_key,
            )
            session.add(proposal)
            await session.flush()
            for note_id in used:
                note = await session.get(EmailNote, note_id)
                if note is not None:
                    note.proposal_id = proposal.id
            count += 1
    return count
