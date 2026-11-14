"""Task 5.3: a human approves with one click and undoes with one click.

Approve: the branch is merged (the pull request on GitHub, or a merge commit in the local repo),
and each prompt Opus changed becomes a new version of that prompt in Settings, through the
release gate like any other change. Undo: a revert commit (history is kept) and the previous
prompt versions go live again.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.db import tenant_session
from core.models import Prompt, PromptVersion
from core.prompts import add_version, live_prompt, make_live
from eval_lab.gate import request_gate
from self_improve import github, repo
from self_improve.models import ImproveRun


class ReviewError(RuntimeError):
    pass


@dataclass
class Decision:
    message: str
    gates_to_run: list[uuid.UUID]


async def approve(
    business_id: uuid.UUID, run_id: uuid.UUID, *, user_id: uuid.UUID, who: str
) -> Decision:
    source = get_settings().self_improve_repo
    async with tenant_session(business_id, user_id=user_id) as session:
        run = await session.get(ImproveRun, run_id)
        if run is None or run.status != "fixed":
            raise ReviewError("Only a fixed run waiting for review can be approved.")
        packet = dict(run.packet)
        code_changed = any(not path.startswith(repo.INPUT_DIR) for path in run.changed_files)
        merge_sha = None
        if run.branch and code_changed:
            if run.pr_number and github.configured():
                merge_sha = await github.merge_pr(run.pr_number, f"Self-Improve: {run.title}")
            else:
                merge_sha = await repo.merge(
                    source,
                    run.branch,
                    into=str(packet.get("base_branch", "main")),
                    message=f"Merge {run.branch}: {run.title}",
                    who=f"{who} (approved in the app)",
                )
        gates: list[uuid.UUID] = []
        versions: dict[str, dict[str, str | None]] = {}
        for key, body in dict(packet.get("prompt_changes") or {}).items():
            live = await live_prompt(session, key)
            version = await add_version(
                session,
                business_id,
                key,
                body,
                author="self_improve",
                status="gating",
                note=f"Self-Improve run {str(run_id)[:8]}",
                created_by=user_id,
                approved_by=user_id,
            )
            gate, needs_run = await request_gate(
                session,
                business_id,
                target_kind="prompt",
                target_key=key,
                candidate_ref=str(version.id),
                baseline_ref=str(live.version_id) if live.version_id else None,
                user_id=user_id,
            )
            if needs_run:
                gates.append(gate.id)
            versions[key] = {
                "new": str(version.id),
                "previous": str(live.version_id) if live.version_id else None,
            }
        run.status, run.decided_by, run.decided_at, run.merge_sha = (
            "approved",
            user_id,
            datetime.now(UTC),
            merge_sha,
        )
        run.packet = packet | {"approved_versions": versions}
    parts: list[str] = []
    if merge_sha:
        parts.append(
            f"Merged ({merge_sha[:8]}). Rebuild to use the new code: docker compose up -d --build."
        )
    if versions:
        parts.append(
            f"{len(versions)} prompt(s) saved as new versions"
            + (", checked by the release gate now." if gates else ", live now.")
        )
    return Decision(" ".join(parts) or "Approved.", gates)


async def reject(business_id: uuid.UUID, run_id: uuid.UUID, *, user_id: uuid.UUID) -> None:
    async with tenant_session(business_id, user_id=user_id) as session:
        run = await session.get(ImproveRun, run_id)
        if run is None or run.status not in ("fixed", "not_fixed"):
            raise ReviewError("This run can't be rejected now.")
        if run.pr_number and github.configured():
            await github.close_pr(run.pr_number)
        run.status, run.decided_by, run.decided_at = "rejected", user_id, datetime.now(UTC)


async def undo(business_id: uuid.UUID, run_id: uuid.UUID, *, user_id: uuid.UUID, who: str) -> str:
    source = get_settings().self_improve_repo
    async with tenant_session(business_id, user_id=user_id) as session:
        run = await session.get(ImproveRun, run_id)
        if run is None or run.status != "approved":
            raise ReviewError("Only an approved run can be undone.")
        packet = dict(run.packet)
        revert_sha = None
        if run.merge_sha:
            if run.pr_number and github.configured():
                revert_sha = await _undo_on_github(run, str(packet.get("base_branch", "main")))
            else:
                revert_sha = await repo.revert(
                    source, run.merge_sha, who=f"{who} (undone in the app)"
                )
        restored = 0
        for change in dict(packet.get("approved_versions") or {}).values():
            new = await session.get(PromptVersion, uuid.UUID(str(change["new"])))
            if new is not None and new.status in ("live", "gating"):
                new.status = "retired"
            previous_id = change.get("previous")
            previous = (
                await session.get(PromptVersion, uuid.UUID(previous_id)) if previous_id else None
            )
            if previous is not None and previous.status != "live":
                await make_live(session, previous)
                restored += 1
            elif previous is None and new is not None:
                await _restore_latest(session, new.prompt_id, exclude=new.id)
        run.status, run.revert_sha = "undone", revert_sha
        run.decided_by, run.decided_at = user_id, datetime.now(UTC)
    return (
        "Undone."
        + (f" Revert commit {revert_sha[:8]}." if revert_sha else "")
        + (f" {restored} prompt(s) back to the version before." if restored else "")
    )


async def _restore_latest(
    session: AsyncSession, prompt_id: uuid.UUID, *, exclude: uuid.UUID
) -> None:
    prompt = await session.get(Prompt, prompt_id)
    if prompt is None:
        return
    latest = (
        await session.execute(
            select(PromptVersion)
            .where(PromptVersion.prompt_id == prompt_id, PromptVersion.id != exclude)
            .order_by(PromptVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None:
        await make_live(session, latest)


async def _undo_on_github(run: ImproveRun, base_branch: str) -> str:
    """A revert pull request, merged at once: GitHub has no one-call undo of a merge."""
    assert run.merge_sha and run.pr_number
    source = get_settings().self_improve_repo
    tip = await github.fetch(source, base_branch)
    branch = f"undo/{str(run.id)[:8]}"
    tree = get_settings().storage_dir / "improve" / "worktrees" / branch.replace("/", "-")
    await repo.git(source, "worktree", "add", "-q", "-b", branch, str(tree), tip)
    try:
        await repo.git(tree, *repo.IDENTITY, "revert", "--no-edit", "-m", "1", run.merge_sha)
        await github.push(source, branch)
    finally:
        await repo.git(source, "worktree", "remove", "--force", str(tree), check=False)
    number, _ = await github.open_pr(
        branch,
        base_branch,
        f"Undo Self-Improve: {run.title}",
        f"Reverts #{run.pr_number}, undone with one click in the app.",
    )
    return await github.merge_pr(number, f"Undo Self-Improve: {run.title}")
