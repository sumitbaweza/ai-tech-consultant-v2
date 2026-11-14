"""The judge (Task 4.1): question + expected answer + the app's answer -> a score and one line of
reason. Claude Opus 5.5 by default, Nemotron 3 Ultra as the free second opinion, both picked in
Settings > Models, both prompts in Settings > Prompts. A model never judges its own answers: if
the judge is the model that answered, the second opinion becomes the judge.

The Self-Improve Loop may never edit this file or the judge's prompts (protected paths).
"""

import uuid
from dataclasses import dataclass

from pydantic import BaseModel, Field
from sqlalchemy import select

from core.db import tenant_session
from core.llm.client import LlmClient
from core.llm.registry import default_model
from core.llm.types import ChatTurn, LlmError, LlmRequest
from core.models import ModelChoice
from core.prompts import live_prompt, render

PASS_SCORE = 0.7


class JudgeReply(BaseModel):
    score: float = Field(ge=0, le=1)
    passed: bool
    reason: str


@dataclass
class Judgement:
    model_key: str
    score: float
    passed: bool
    reason: str


async def judges_for(business_id: uuid.UUID) -> tuple[str, str]:
    async with tenant_session(business_id) as session:
        chosen = {
            c.step: c.model_key for c in (await session.execute(select(ModelChoice))).scalars()
        }
    return chosen.get("judge", default_model("judge")), chosen.get(
        "judge_second", default_model("judge_second")
    )


async def judge(
    business_id: uuid.UUID,
    *,
    question: str,
    expected: str,
    answer: str,
    answered_by: str | None,
    trace_id: uuid.UUID | None = None,
    second: bool = False,
    llm: LlmClient | None = None,
) -> Judgement | None:
    """None when no judge model is available (no key): the case stays unjudged, never guessed."""
    main, backup = await judges_for(business_id)
    model = backup if second else main
    if answered_by is not None and model == answered_by:
        model = main if second else backup  # never judge your own answer
        if model == answered_by:
            return None
    async with tenant_session(business_id) as session:
        system = await live_prompt(session, "eval.judge")
        user = await live_prompt(session, "eval.judge_user")
    try:
        result = await (llm or LlmClient(business_id)).call(
            LlmRequest(
                business_id=business_id,
                step="eval.judge_second" if second else "eval.judge",
                job="judge_second" if second else "judge",
                system=[system.body],
                messages=[
                    ChatTurn(
                        "user",
                        render(
                            user.body,
                            {"question": question, "expected": expected, "answer": answer},
                        ),
                    )
                ],
                schema=JudgeReply,
                max_tokens=8000,
                effort="medium",
                model_key=model,
                trace_id=trace_id,
                prompt_key=system.key,
                prompt_version_id=system.version_id,
            )
        )
    except LlmError:
        return None
    reply = result.parsed
    if not isinstance(reply, JudgeReply):
        return None
    return Judgement(
        model_key=result.model_key,
        score=reply.score,
        passed=reply.score >= PASS_SCORE,
        reason=reply.reason.strip()[:300],
    )
