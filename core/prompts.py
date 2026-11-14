"""Every prompt the app sends to a model lives here, editable in Settings (Task 1.3).

The default text of each prompt is a file, prompts/<key>/v<N>.md, with a small header naming
its variables. Each business gets a copy as version 1 when it is created; every save in
Settings is a new version, and exactly one version is live. Restoring an old version (or the
default) copies it into a new version, so history is never rewritten.

Variables are written {like_this}. A prompt's header lists the ones it must keep; Settings
warns before saving a version that drops one. {{ and }} stay literal braces.
"""

import difflib
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import REPO_ROOT
from core.models import Prompt, PromptVersion

PROMPTS_DIR = REPO_ROOT / "prompts"
_VARIABLE = re.compile(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})")


@dataclass(frozen=True)
class DefaultPrompt:
    key: str
    version: int
    title: str
    description: str
    kind: str  # system | user_template
    required: tuple[str, ...]
    advanced: bool
    body: str
    path: Path


@dataclass(frozen=True)
class LivePrompt:
    key: str
    body: str
    version_id: uuid.UUID | None  # None when it came from the file (no business copy yet)
    version: int


def _parse(path: Path) -> DefaultPrompt:
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not match:
        raise ValueError(f"{path} has no header")
    header = cast(dict[str, Any], yaml.safe_load(match.group(1)) or {})
    key = path.parent.name
    return DefaultPrompt(
        key=key,
        version=int(path.stem.lstrip("v")),
        title=str(header["title"]),
        description=str(header.get("description", "")),
        kind=str(header.get("kind", "system")),
        required=tuple(str(v) for v in header.get("required", [])),
        advanced=bool(header.get("advanced", False)),
        body=match.group(2).strip() + "\n",
        path=path,
    )


@lru_cache
def defaults() -> dict[str, DefaultPrompt]:
    """The newest default version of every prompt, by key."""
    found: dict[str, DefaultPrompt] = {}
    for path in sorted(PROMPTS_DIR.glob("*/v*.md")):
        prompt = _parse(path)
        if prompt.key not in found or prompt.version > found[prompt.key].version:
            found[prompt.key] = prompt
    return found


def variables_in(text: str) -> list[str]:
    seen: list[str] = []
    for name in _VARIABLE.findall(text):
        if name not in seen:
            seen.append(name)
    return seen


def missing_variables(text: str, required: tuple[str, ...] | list[str]) -> list[str]:
    present = set(variables_in(text))
    return [name for name in required if name not in present]


def render(text: str, values: dict[str, str]) -> str:
    """Fill {variables}. Unknown ones are left as they are, so a typo shows in the output."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        return values[name] if name in values else match.group(0)

    filled = _VARIABLE.sub(fill, text)
    return filled.replace("{{", "{").replace("}}", "}")


def diff(old: str, new: str, *, old_label: str = "before", new_label: str = "after") -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=old_label,
            tofile=new_label,
        )
    )


# ---------------------------------------------------------------- per-business versions


async def seed_business_prompts(session: AsyncSession, business_id: uuid.UUID) -> int:
    """Give a new business version 1 of every default prompt, live."""
    count = 0
    for default in defaults().values():
        prompt = Prompt(
            business_id=business_id,
            key=default.key,
            title=default.title,
            description=default.description,
            kind=default.kind,
            required_vars=list(default.required),
            advanced=default.advanced,
        )
        session.add(prompt)
        await session.flush()
        session.add(
            PromptVersion(
                business_id=business_id,
                prompt_id=prompt.id,
                version=1,
                body=default.body,
                variables=variables_in(default.body),
                author="default",
                status="live",
                note=f"Default from {default.path.relative_to(REPO_ROOT).as_posix()}",
            )
        )
        count += 1
    await session.flush()
    return count


async def live_prompt(session: AsyncSession, key: str) -> LivePrompt:
    """The business's live version of a prompt (row-level security picks the business)."""
    row = (
        await session.execute(
            select(PromptVersion.id, PromptVersion.body, PromptVersion.version)
            .join(Prompt, Prompt.id == PromptVersion.prompt_id)
            .where(Prompt.key == key, PromptVersion.status == "live")
        )
    ).first()
    if row is not None:
        return LivePrompt(key=key, body=row.body, version_id=row.id, version=row.version)
    default = defaults().get(key)
    if default is None:
        raise KeyError(f"No prompt {key!r}")
    return LivePrompt(key=key, body=default.body, version_id=None, version=0)


async def add_version(
    session: AsyncSession,
    business_id: uuid.UUID,
    key: str,
    body: str,
    *,
    author: str,
    status: str,
    note: str | None = None,
    created_by: uuid.UUID | None = None,
    approved_by: uuid.UUID | None = None,
) -> PromptVersion:
    """Save a new version. With status 'live', the old live version is retired first."""
    prompt = (await session.execute(select(Prompt).where(Prompt.key == key))).scalar_one()
    next_version = (
        await session.execute(
            select(func.coalesce(func.max(PromptVersion.version), 0) + 1).where(
                PromptVersion.prompt_id == prompt.id
            )
        )
    ).scalar_one()
    if status == "live":
        await retire_live(session, prompt.id)
    version = PromptVersion(
        business_id=business_id,
        prompt_id=prompt.id,
        version=next_version,
        body=body,
        variables=variables_in(body),
        author=author,
        status=status,
        note=note,
        created_by=created_by,
        approved_by=approved_by,
    )
    session.add(version)
    await session.flush()
    return version


async def retire_live(session: AsyncSession, prompt_id: uuid.UUID) -> None:
    await session.execute(
        update(PromptVersion)
        .where(PromptVersion.prompt_id == prompt_id, PromptVersion.status == "live")
        .values(status="retired")
    )


async def make_live(session: AsyncSession, version: PromptVersion) -> None:
    """Promote a version (after the release gate passed, or on a one-click restore)."""
    await retire_live(session, version.prompt_id)
    version.status = "live"
    await session.flush()
