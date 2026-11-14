"""Task 5.1: pack what Opus needs: the comment, the prompts, the failing answers and the
related code. Also decides which failures are worth sending to Opus at all.

The packet is saved on the run (improve.runs.packet), so the screen shows exactly what was sent.
"""

import uuid
from typing import Any

from sqlalchemy import select

from core.db import tenant_session
from core.prompts import live_prompt
from eval_lab.models import Benchmark, Case, Result, Run

# A human fixes these, not Opus (the brief: "which failures are worth sending to Opus")
FOR_A_HUMAN = {
    "trick": "a safety rule (the injection shield): changes there need a person's judgement",
    "handoff": "a safety rule (hand-off to a human): never tuned by a model",
    "teach": "teach tests check the lesson flow, which needs a person's correction",
}
NO_MODEL = "the URL cases call no model; a code bug there needs a person"

# The prompts each job's answers used, by key
PROMPTS_FOR_JOB = {
    "chat_answer": (
        "chat.master_system",
        "chat.answer_user",
        "chat.find_facts",
        "chat.extract_question",
    ),
    "brain_answer": ("brain.answer_system", "brain.answer_user"),
}
CODE_FOR_JOB = {
    "chat_answer": (
        "ask_teach_chat/pipeline.py",
        "core/answers.py",
        "knowledge_brain/answer.py",
        "knowledge_brain/search/search.py",
    ),
    "brain_answer": (
        "knowledge_brain/answer.py",
        "core/answers.py",
        "knowledge_brain/search/search.py",
        "knowledge_brain/search/chunk.py",
    ),
}
CODE_FOR_CATEGORY = {
    "screenshot": ("knowledge_brain/images.py",),
    "synonym_typo": ("knowledge_brain/search/index.py",),
    "no_answer": ("knowledge_brain/answer.py",),
    "not_in_kb": ("ask_teach_chat/pipeline.py",),
}
JOB_FOR_KIND = {
    "brain": "brain_answer",
    "hidden": "brain_answer",
    "chat": "chat_answer",
    "feedback": "chat_answer",
    "starter": "chat_answer",
}


def not_worth_sending(category: str, result: Result) -> str | None:
    """Why a failure should go to a person instead of Opus, or None to send it."""
    if category.startswith("url_"):
        return NO_MODEL
    if category in FOR_A_HUMAN:
        return FOR_A_HUMAN[category]
    if result.error:
        return "the run had an error (a key, a timeout): fix the setup, not the app"
    if result.passed is not False:
        return "this case did not fail"
    return None


def default_comment(failing: list[dict[str, Any]]) -> str:
    reasons = [f"{f['case_key']}: {f['why']}" for f in failing if f.get("why")]
    return "The judge's reasons:\n" + "\n".join(reasons) if reasons else ""


async def build_packet(
    business_id: uuid.UUID,
    *,
    source_run_id: uuid.UUID,
    comment: str,
    result_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    async with tenant_session(business_id) as session:
        run = await session.get(Run, source_run_id)
        if run is None:
            raise LookupError("That eval run doesn't exist.")
        benchmark = await session.get(Benchmark, run.benchmark_id)
        query = (
            select(Result, Case)
            .join(Case, Case.id == Result.case_id)
            .where(Result.run_id == source_run_id)
        )
        if result_ids:
            query = query.where(Result.id.in_(result_ids))
        rows = (await session.execute(query.order_by(Case.case_key))).all()
        job = (
            run.job
            if run.job in PROMPTS_FOR_JOB
            else JOB_FOR_KIND.get(benchmark.kind if benchmark else "", "chat_answer")
        )
        prompts = {key: (await live_prompt(session, key)).body for key in PROMPTS_FOR_JOB[job]}
    failing: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for result, case in rows:
        reason = not_worth_sending(case.category, result)
        if reason:
            if result.passed is False or result.error:
                excluded.append({"case_key": case.case_key, "reason": reason})
            continue
        failing.append(
            {
                "result_id": str(result.id),
                "case_id": str(case.id),
                "case_key": case.case_key,
                "category": case.category,
                "question": case.question,
                "expected": case.expected,
                "answer": result.answer or "",
                "why": result.judge_reason or _failed_checks(result.rule_checks),
                "model_key": result.model_key,
            }
        )
    model_keys = sorted({f["model_key"] for f in failing})
    model_key = model_keys[0] if model_keys else None
    failing = [
        f for f in failing if f["model_key"] == model_key
    ]  # one model per run: fair before/after
    files = list(CODE_FOR_JOB[job])
    for category in sorted({f["category"] for f in failing}):
        files += [p for p in CODE_FOR_CATEGORY.get(category, ()) if p not in files]
    return {
        "source_run_id": str(source_run_id),
        "job": job,
        "model_key": model_key,
        "comment": comment.strip() or default_comment(failing),
        "failing": failing,
        "excluded": excluded,
        "prompts": prompts,
        "related_files": files,
    }


def _failed_checks(checks: dict[str, Any]) -> str:
    failed = [
        name.replace("_", " ")
        for name, ok in checks.items()
        if not name.startswith("_") and ok is False
    ]
    return "failed the rule check(s): " + ", ".join(failed) if failed else ""


def failing_cases_text(packet: dict[str, Any]) -> str:
    blocks: list[str] = []
    for f in packet["failing"]:
        blocks.append(
            f"### {f['case_key']} ({f['category'].replace('_', ' ')})\n"
            f"Question: {f['question']}\nExpected: {f['expected']}\n"
            f"The app answered: {f['answer'][:1500]}\nWhy it failed: {f['why'] or 'not given'}"
        )
    return "\n\n".join(blocks)


def related_files_text(packet: dict[str, Any]) -> str:
    return "\n".join(f"- {path}" for path in packet["related_files"])
