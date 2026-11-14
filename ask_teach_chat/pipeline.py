"""One answer in the Ask and Teach Chat (Tasks 3.1 to 3.3, 3.6).

Each step is a plain function, and each shows in the trace panel with its time:
1. safety: the injection shield's rules and the hand-off rules (ask_teach_chat/safety.py)
2. hand-off: angry, refund and legal messages (and the role's own rule) go to a human
3. question: a long pasted email is reduced to the real question(s)
4. knowledge: the Brain's API returns the re-ranked knowledge, the role's instructions, the
   right skill and the approved lessons (POST /v1/context)
5. "I don't know": below the business's line, no answer is written; a human expert is asked
6. write: the agent writes its own prompt from the Settings prompts, the role and the skill,
   and the router picks the model (Task 3.2: users never write prompts)
7. check: citations must point at sources it was given; the output check stops leaks
"""

import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import select

from ask_teach_chat import safety
from core.answers import WrittenAnswer, check_citations, source_block, strip_invented
from core.brain_api import ContextRequest, ContextResponse, ExpertTaskIn, SearchHit
from core.brain_client import BrainClient, BrainError
from core.db import tenant_session
from core.llm.client import LlmClient
from core.llm.types import (
    ChatTurn,
    LlmError,
    LlmRequest,
    LlmResult,
    MissingConnection,
    ModelRefused,
)
from core.models import Business, ModelCall, RoleVersion
from core.prompts import LivePrompt, live_prompt, render
from core.tracing import Tracer

Kind = Literal["answer", "dont_know", "handoff", "refused"]
LONG_MESSAGE = 600  # characters: a pasted email; the real question is extracted first
KNOWLEDGE = (
    "sitemap",
    "page_url",
    "instruction",
    "skill",
    "past_email",
    "document",
    "lesson",
    "expert",
)


@dataclass
class RoleSettings:
    """What the pipeline needs from a role: its live version, or the editor's unsaved values."""

    role_id: uuid.UUID | None
    version_id: uuid.UUID | None
    name: str
    goal: str
    prompt: str
    tone: str
    capabilities: list[str]
    knowledge_types: list[str]
    skill_keys: list[str]
    model_key: str | None
    handover_note: str = ""

    @classmethod
    def from_version(cls, name: str, version: RoleVersion) -> RoleSettings:
        return cls(
            role_id=version.role_id,
            version_id=version.id,
            name=name,
            goal=version.goal,
            prompt=version.prompt,
            tone=version.tone,
            capabilities=list(version.capabilities),
            knowledge_types=list(version.knowledge_types),
            skill_keys=list(version.skill_keys),
            model_key=version.model_key,
            handover_note=str(version.handover.get("note", "")),
        )


@dataclass
class Overrides:
    """Ways to answer with something not yet live: a candidate prompt (the release gate), an
    unsaved skill (its editor), a forced model (an eval run)."""

    prompts: dict[str, str] = field(default_factory=dict[str, str])
    skill_markdown: str | None = None
    skill_version_id: uuid.UUID | None = None
    model_key: str | None = None
    include_lessons: bool = True
    caching: bool = True
    local_only: bool = False  # the Eval Lab's local-only runs


@dataclass
class Outcome:
    kind: Kind
    text: str
    question: str
    trace_id: uuid.UUID
    citations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    knowledge_types: list[str] = field(default_factory=list[str])
    chunk_ids: list[str] = field(default_factory=list[str])
    skill_version_ids: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    lesson_ids: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    instruction_ids: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    confidence: float | None = None
    top_score: float | None = None
    model_key: str | None = None
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0
    needs_human: bool = False
    handoff_reason: str | None = None
    expert_task_id: uuid.UUID | None = None
    safety: dict[str, Any] = field(default_factory=dict[str, Any])
    prompt_versions: dict[str, str] = field(default_factory=dict[str, str])
    skill_name: str | None = None
    lesson_titles: list[str] = field(default_factory=list[str])
    steps: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    @property
    def answered(self) -> bool:
        return self.kind == "answer"


class RiskCheck(BaseModel):
    risk: str
    injection: bool
    reason: str


class FoundQuestions(BaseModel):
    questions: list[str]
    summary: str
    customer_name: str


@dataclass
class _Run:
    business_id: uuid.UUID
    llm: LlmClient
    tracer: Tracer
    overrides: Overrides
    prompts_used: dict[str, str] = field(default_factory=dict[str, str])
    # Eval cases and role tests never open expert tasks: their questions are tests, not customers
    opens_tasks: bool = True

    async def prompt(self, key: str) -> LivePrompt:
        if key in self.overrides.prompts:
            return LivePrompt(
                key=key, body=self.overrides.prompts[key], version_id=None, version=-1
            )
        async with tenant_session(self.business_id) as session:
            found = await live_prompt(session, key)
        if found.version_id:
            self.prompts_used[key] = str(found.version_id)
        return found


async def answer(
    business_id: uuid.UUID,
    *,
    text: str,
    role: RoleSettings,
    history: list[ChatTurn] | None = None,
    chat_id: uuid.UUID | None = None,
    overrides: Overrides | None = None,
    trace_kind: str = "chat_answer",
    real_customer_data: bool = True,
    llm: LlmClient | None = None,
) -> Outcome:
    started = time.monotonic()
    overrides = overrides or Overrides()
    tracer = await Tracer.start(business_id, trace_kind, text[:120])
    run = _Run(
        business_id,
        llm or LlmClient(business_id, force_local=overrides.local_only),
        tracer,
        overrides,
        opens_tasks=trace_kind not in ("eval_case", "role_test", "skill_test", "setup"),
    )
    async with tenant_session(business_id) as session:
        business = await session.get(Business, business_id)
        assert business is not None
        business_name, tone, threshold = business.name, business.tone, business.dont_know_threshold
    try:
        outcome = await _answer(
            run,
            text,
            role,
            history or [],
            chat_id,
            business_name,
            tone,
            threshold,
            real_customer_data,
        )
    except Exception:
        await tracer.finish("error")
        raise
    outcome.latency_ms = int((time.monotonic() - started) * 1000)
    outcome.prompt_versions = run.prompts_used
    outcome.steps = [{"name": s.name, "ms": s.duration_ms, **s.attrs} for s in tracer.spans]
    async with tenant_session(business_id) as session:
        costs = await session.execute(
            select(ModelCall.cost_usd).where(ModelCall.trace_id == tracer.trace_id)
        )
        outcome.cost_usd = sum((Decimal(c) for c in costs.scalars()), Decimal(0))
    await tracer.finish("ok")
    return outcome


async def _answer(
    run: _Run,
    text: str,
    role: RoleSettings,
    history: list[ChatTurn],
    chat_id: uuid.UUID | None,
    business_name: str,
    tone: str,
    threshold: float,
    real_data: bool,
) -> Outcome:
    trace_id = run.tracer.trace_id
    # 1. Safety rules, and the cheap model for unclear risk
    with run.tracer.span("safety") as span:
        scan = safety.scan(text)
        if scan.unsure:
            scan.risk = await _risk_by_model(run, text, scan, real_data)
        span.update(scan.as_dict())
    # 2. Hand-off: angry, refund, legal always; then the role's own rule
    reason: str | None = scan.risk if scan.risk != "none" else None
    if reason is None and role.handover_note and _role_rule_applies(role.handover_note, text):
        reason = "role"
    if reason is not None:
        with run.tracer.span("hand_off", reason=reason):
            task_id = await _expert_task(run, text, f"handoff_{reason}", chat_id, role)
        note = (
            f"{safety.HANDOFF_TEXT[reason]}\n\nSuggested holding reply:\n\n> {safety.HOLDING_REPLY}"
        )
        return Outcome(
            kind="handoff",
            text=note,
            question=text[:500],
            trace_id=trace_id,
            needs_human=True,
            handoff_reason=reason,
            expert_task_id=task_id,
            safety=scan.as_dict(),
        )
    # 3. The real question inside a long message
    with run.tracer.span("find_question") as span:
        question = await _find_question(run, text, real_data)
        span.update(question=question[:200])
    if scan.injection and not question.strip():
        return Outcome(
            kind="refused",
            text=safety.REFUSAL,
            question="",
            trace_id=trace_id,
            safety=scan.as_dict(),
        )
    # 4. Knowledge from the Brain, through its API
    with run.tracer.span("knowledge") as span:
        context = await _context(run, question, role)
        span.update(
            hits=len(context.hits),
            top_score=context.top_score,
            types=sorted({h.knowledge_type for h in context.hits}),
            skill=context.skill.name if context.skill else None,
            lessons=len(context.lessons),
        )
    if run.overrides.skill_markdown is not None:
        skill_text, skill_name = run.overrides.skill_markdown, "the skill being edited"
    else:
        skill_text = context.skill.markdown if context.skill else ""
        skill_name = context.skill.name if context.skill else None
    # 5. The "I don't know" line, set by the Eval Lab's sweep
    if not context.hits or context.top_score < threshold:
        with run.tracer.span("dont_know", top_score=context.top_score, line=threshold):
            task_id = await _expert_task(run, question, "dont_know", chat_id, role)
        return Outcome(
            kind="dont_know",
            question=question,
            trace_id=trace_id,
            top_score=context.top_score,
            text=(
                "I don't know: our knowledge doesn't answer this yet. I've asked a human expert; "
                "their answer will be used next time."
            ),
            needs_human=True,
            expert_task_id=task_id,
            safety=scan.as_dict(),
        )
    # 6. Write: Settings prompts + role + instructions (cached), then skill, lessons, knowledge
    canary = safety.new_canary()
    master, user = await run.prompt("chat.master_system"), await run.prompt("chat.answer_user")
    system_blocks = [
        render(master.body, {"business_name": business_name, "tone": tone, "canary": canary}),
        _role_block(role, tone),
        _instructions_block(context),
    ]
    sources, numbered = source_block(context.hits)
    message = render(
        user.body,
        {
            "skill": skill_text or "(no skill fits this question)",
            "lessons": "\n\n".join(
                f"Lesson ({lesson.updated_at:%Y-%m-%d}): {lesson.markdown}"
                if lesson.updated_at
                else lesson.markdown
                for lesson in context.lessons
            )
            or "(none)",
            "knowledge_results": sources,
            "chat_history": "\n".join(f"{t.role}: {t.text[:1500]}" for t in history[-6:])
            or "(start of the chat)",
            "customer_message": text,
            "question": question,
        },
    )
    with run.tracer.span("write_answer") as span:
        result = await _write(run, system_blocks, message, numbered, role, master, real_data)
        span.update(
            model=result.model_key,
            escalated=result.route.escalated_from,
            tokens=result.usage.input_tokens,
        )
    written = result.parsed
    assert isinstance(written, WrittenAnswer)
    # 7. Checks: citations, and the output shield
    with run.tracer.span("check") as span:
        valid, invented = check_citations(written, numbered)
        why = safety.leaked(written.answer, canary, system_blocks[0])
        span.update(valid_citations=valid, invented=invented, leak=why)
    if why is not None:
        return Outcome(
            kind="refused",
            text=safety.REFUSAL,
            question=question,
            trace_id=trace_id,
            model_key=result.model_key,
            safety={**scan.as_dict(), "output_blocked": why},
        )
    if not written.answered or not valid:
        task_id = await _expert_task(run, question, "dont_know", chat_id, role)
        return Outcome(
            kind="dont_know",
            question=question,
            trace_id=trace_id,
            model_key=result.model_key,
            text=written.answer
            if not written.answered
            else "I don't know: I couldn't find this in our knowledge. I've asked a human expert.",
            top_score=context.top_score,
            confidence=written.confidence,
            needs_human=True,
            expert_task_id=task_id,
            safety=scan.as_dict(),
        )
    used = [numbered[sid] for sid in valid]
    return Outcome(
        kind="answer",
        text=strip_invented(written.answer, invented),
        question=question,
        trace_id=trace_id,
        citations=[_citation(sid, hit) for sid, hit in zip(valid, used, strict=True)],
        knowledge_types=list(dict.fromkeys(hit.knowledge_type for hit in used)),
        chunk_ids=[str(hit.chunk_id) for hit in used],
        skill_version_ids=[context.skill.version_id]
        if context.skill and run.overrides.skill_markdown is None
        else [],
        lesson_ids=[lesson.id for lesson in context.lessons],
        instruction_ids=[i.id for i in context.instructions],
        confidence=written.confidence,
        top_score=context.top_score,
        model_key=result.model_key,
        needs_human=written.needs_human,
        handoff_reason="model" if written.needs_human else None,
        safety=scan.as_dict(),
        skill_name=skill_name,
        lesson_titles=[lesson.title for lesson in context.lessons],
    )


def _citation(sid: str, hit: SearchHit) -> dict[str, Any]:
    return {
        "n": sid,
        "chunk_id": hit.chunk_id,
        "knowledge_type": hit.knowledge_type,
        "title": hit.title,
        "url": hit.url,
        "anchor": hit.anchor,
        "quote": hit.content[:300],
    }


def _role_block(role: RoleSettings, tone: str) -> str:
    may = ", ".join(role.capabilities) or "answer from the knowledge"
    return (
        f'<role name="{role.name}">\nGoal: {role.goal}\nTone: {role.tone}; the business\'s tone: {tone}\n'
        f"What you may do: {may}\n\n{role.prompt}\n</role>"
    )


def _instructions_block(context: ContextResponse) -> str:
    rules = "\n".join(f"- {i.title}: {i.body}" for i in context.instructions)
    return f"<instructions>\n{rules or '(none)'}\n</instructions>"


def _role_rule_applies(note: str, text: str) -> bool:
    """A role's hand-over note names topics ("custom contracts, discounts"); a message that
    names one of them goes to a human."""
    topics = [
        t.strip().lower()
        for t in re.split(r"[,;]|\bor\b|\band\b", note.split(":")[-1])
        if len(t.strip()) > 3
    ]
    lowered = text.lower()
    return any(
        re.search(rf"\b{re.escape(topic)}\b", lowered)
        for topic in topics
        if len(topic.split()) <= 3
    )


async def _risk_by_model(
    run: _Run, text: str, scan: safety.SafetyScan, real_data: bool
) -> safety.Risk:
    prompt = await run.prompt("chat.risk_check")
    try:
        result = await run.llm.call(
            LlmRequest(
                business_id=run.business_id,
                step="chat.risk_check",
                job="tag_job",
                system=[prompt.body],
                messages=[ChatTurn("user", text[:6000])],
                schema=RiskCheck,
                max_tokens=2000,
                effort="low",
                complexity="low",
                trace_id=run.tracer.trace_id,
                prompt_key=prompt.key,
                prompt_version_id=prompt.version_id,
                real_customer_data=real_data,
            )
        )
    except LlmError, MissingConnection:
        return (
            "refund"
            if re.search(r"\b(charge[ds]?|billing|payment|cancel)", text, re.I)
            else "angry"
        )
    parsed = result.parsed
    if isinstance(parsed, RiskCheck):
        if parsed.injection:
            scan.injection, scan.injection_hits = (
                True,
                [*scan.injection_hits, f"model: {parsed.reason}"],
            )
        if parsed.risk in ("angry", "refund", "legal"):
            return parsed.risk  # type: ignore[return-value]
    return "none"


def _looks_like_email(text: str) -> bool:
    return len(text) > LONG_MESSAGE or bool(
        re.search(r"^(from|subject|to|sent|date):|wrote:|^>", text, re.I | re.M)
    )


async def _find_question(run: _Run, text: str, real_data: bool) -> str:
    if not _looks_like_email(text):
        return text.strip()
    prompt = await run.prompt("chat.extract_question")
    try:
        result = await run.llm.call(
            LlmRequest(
                business_id=run.business_id,
                step="chat.extract_question",
                job="tag_job",
                system=[prompt.body],
                messages=[ChatTurn("user", text[:12000])],
                schema=FoundQuestions,
                max_tokens=3000,
                effort="low",
                complexity="low",
                trace_id=run.tracer.trace_id,
                prompt_key=prompt.key,
                prompt_version_id=prompt.version_id,
                real_customer_data=real_data,
            )
        )
        if isinstance(result.parsed, FoundQuestions) and result.parsed.questions:
            return " ".join(result.parsed.questions[:3])
    except LlmError:
        pass
    # No model: the sentences with question marks, else the first real paragraph
    asked = re.findall(r"[^.?!\n]*\?", text)
    if asked:
        return " ".join(q.strip() for q in asked[:3])
    paragraphs = [
        p.strip()
        for p in text.split("\n\n")
        if len(p.strip()) > 20 and not p.strip().lower().startswith(("hi", "hello", "dear"))
    ]
    return paragraphs[0][:500] if paragraphs else text[:500]


async def _context(run: _Run, question: str, role: RoleSettings) -> ContextResponse:
    knowledge = [k for k in role.knowledge_types if k in KNOWLEDGE]
    request = ContextRequest(
        question=question[:8000],
        role_id=role.role_id,
        knowledge_types=knowledge or None,  # type: ignore[arg-type]
        skill_keys=role.skill_keys or None,
        skill_version_id=run.overrides.skill_version_id,
        include_lessons=run.overrides.include_lessons and "lesson" in role.knowledge_types,
        local_only=run.overrides.local_only,
    )
    async with await BrainClient.for_business(run.business_id) as brain:
        return await brain.context(request)


async def _write(
    run: _Run,
    system_blocks: list[str],
    message: str,
    numbered: dict[str, SearchHit],
    role: RoleSettings,
    master: LivePrompt,
    real_data: bool,
) -> LlmResult:
    def quick_check(result: LlmResult) -> str | None:
        """Task 4.3: if the answer fails this, the router tries one model higher."""
        written = result.parsed
        if not isinstance(written, WrittenAnswer):
            return "not valid JSON"
        valid, _ = check_citations(written, numbered)
        if written.answered and not valid:
            return "answered without citing a source it was given"
        return None

    request = LlmRequest(
        business_id=run.business_id,
        step="chat.write_answer",
        job="write_answer",
        system=system_blocks,
        messages=[ChatTurn("user", message)],
        schema=WrittenAnswer,
        max_tokens=8000,
        effort="medium",
        cache_system=run.overrides.caching,
        model_key=run.overrides.model_key or role.model_key,
        trace_id=run.tracer.trace_id,
        prompt_key=master.key,
        prompt_version_id=master.version_id,
        role_version_id=role.version_id,
        real_customer_data=real_data,
    )
    try:
        return await run.llm.call(request, check=quick_check)
    except ModelRefused:
        raise


async def _expert_task(
    run: _Run,
    question: str,
    reason: str,
    chat_id: uuid.UUID | None,
    role: RoleSettings,
) -> uuid.UUID | None:
    if not run.opens_tasks:
        return None
    context = {"role": role.name}
    if chat_id:
        context["chat_id"] = str(chat_id)
    try:
        async with await BrainClient.for_business(run.business_id) as brain:
            task = await brain.create_expert_task(
                ExpertTaskIn(question=question[:8000], reason=reason, context=context)
            )
        return uuid.UUID(str(task["id"]))
    except BrainError:
        return None
