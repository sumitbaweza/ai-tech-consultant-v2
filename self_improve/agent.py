"""Claude Opus 5.5 at work, through the Claude Agent SDK, fenced in.

Opus gets file tools and a shell, confined to its export folder by a PreToolUse hook (`guard`)
that every tool call passes through, reads included:
- file tools may only touch paths inside the folder, never the hidden set's path;
- the shell runs only an allowlist of commands (tests, lint, reading files), with no paths
  outside the folder, no pipes, no variables, no network tools;
- its environment carries the Claude key and nothing else secret: database passwords and app
  keys are blanked (the SDK passes the worker's environment on otherwise).
Every call, allowed or denied, is logged in the round for the reviewer.
"""

import os
import re
import shlex
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from self_improve.repo import HIDDEN

MODEL = "claude-opus-5-5"
TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
SAFE_COMMANDS = (
    "pytest",
    "python -m pytest",
    "ruff",
    "pyright",
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "wc",
    "sed -n",
    "diff",
    "git diff",
    "git status",
    "git log",
)
# Shell features that could reach outside the folder or hide what runs
FORBIDDEN = ("..", "~", "$", "`", "|", ";", "&", ">", "<", "\n")
SECRET_NAME = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|DATABASE_URL", re.IGNORECASE)


@dataclass
class AgentRound:
    summary: str
    cost_usd: Decimal
    tool_calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    error: str | None = None


class AgentRunner(Protocol):
    async def run_round(
        self, work: Path, brief: str, *, budget_usd: Decimal, api_key: str
    ) -> AgentRound: ...


def _inside(path: str, work: Path) -> bool:
    try:
        resolved = (work / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    except OSError, ValueError:
        return False
    root = work.resolve()
    relative = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else None
    return relative is not None and not (relative + "/").startswith(HIDDEN)


def check_tool(name: str, tool_input: dict[str, Any], work: Path) -> str | None:
    """Why this tool call is denied, or None to allow it."""
    if name in ("Read", "Edit", "Write"):
        path = str(tool_input.get("file_path", ""))
        return None if path and _inside(path, work) else f"{name} is limited to the work folder"
    if name in ("Glob", "Grep"):
        path = str(tool_input.get("path") or ".")
        pattern = str(tool_input.get("pattern", ""))
        if not _inside(path, work) or ".." in pattern or pattern.startswith("/"):
            return f"{name} is limited to the work folder"
        return None
    if name == "Bash":
        command = str(tool_input.get("command", "")).strip()
        if any(token in command for token in FORBIDDEN):
            return "Only plain commands: no pipes, redirects, variables, chaining or parent folders"
        if not any(command == safe or command.startswith(safe + " ") for safe in SAFE_COMMANDS):
            return "Allowed commands: " + ", ".join(SAFE_COMMANDS)
        try:
            words = shlex.split(command)
        except ValueError:
            return "The command couldn't be read"
        if any(word.startswith("/") and not _inside(word, work) for word in words):
            return "Paths must be inside the work folder"
        return None
    return f"{name} is not available here"


def agent_env(api_key: str, home: Path) -> dict[str, str]:
    """Blank every secret the worker holds; give Opus only its own key."""
    env = {name: "" for name in os.environ if SECRET_NAME.search(name)}
    env |= {
        "ANTHROPIC_API_KEY": api_key,
        "HOME": str(home),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DB_HOST": "unreachable.invalid",
    }
    return env


class ClaudeAgentRunner:
    """One round = one Agent SDK session: Opus reads the brief, edits, runs the tests, stops."""

    def __init__(self, max_turns: int = 40) -> None:
        self.max_turns = max_turns

    async def run_round(
        self, work: Path, brief: str, *, budget_usd: Decimal, api_key: str
    ) -> AgentRound:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKError,
            HookContext,
            HookMatcher,
            ResultMessage,
            TextBlock,
            query,
        )
        from claude_agent_sdk.types import HookInput, HookJSONOutput

        log: list[dict[str, Any]] = []

        async def guard(
            input_data: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            data = cast(dict[str, Any], input_data)
            name, tool_input = (
                str(data.get("tool_name", "")),
                cast(dict[str, Any], data.get("tool_input") or {}),
            )
            reason = check_tool(name, tool_input, work)
            log.append({"tool": name, "input": _short(tool_input), "denied": reason})
            if reason is None:
                return {}
            return cast(
                HookJSONOutput,
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                },
            )

        async def prompt() -> AsyncIterator[dict[str, Any]]:
            # Hooks need the streaming form of the prompt
            yield {
                "type": "user",
                "message": {"role": "user", "content": brief},
                "parent_tool_use_id": None,
            }

        home = work.parent / f"{work.name}-home"
        home.mkdir(exist_ok=True)
        options = ClaudeAgentOptions(
            model=MODEL,
            cwd=str(work),
            tools=TOOLS,
            allowed_tools=TOOLS,
            permission_mode="acceptEdits",
            max_turns=self.max_turns,
            max_budget_usd=float(budget_usd),
            setting_sources=[],
            env=agent_env(api_key, home),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[guard])]},
        )
        said: list[str] = []
        cost, error = Decimal(0), None
        try:
            async for message in query(prompt=prompt(), options=options):
                if isinstance(message, AssistantMessage):  # tool calls are logged by the guard
                    said += [
                        block.text for block in message.content if isinstance(block, TextBlock)
                    ]
                elif isinstance(message, ResultMessage):
                    cost = Decimal(str(message.total_cost_usd or 0))
                    if message.is_error:
                        error = f"Opus stopped: {message.subtype}"
        except ClaudeSDKError as exc:
            error = f"The Agent SDK failed: {exc}"[:500]
        changes = work / "self_improve_input" / "CHANGES.md"
        summary = (
            changes.read_text(encoding="utf-8")[:4000]
            if changes.exists()
            else "\n".join(said[-3:])[:4000]
        )
        return AgentRound(summary=summary, cost_usd=cost, tool_calls=log, error=error)


FAKE_RULE = "\n\nAnswer with the exact steps from the cited source, in the same order, and name the screen each step is on.\n"


def fake_prompt_fix(work: Path) -> str:
    """What the fake agent does: one general instruction added to the first prompt."""
    prompts = sorted((work / "self_improve_input" / "prompts").glob("*.md"))
    if not prompts:
        return "Nothing to change."
    prompts[0].write_text(
        prompts[0].read_text(encoding="utf-8").rstrip() + FAKE_RULE, encoding="utf-8"
    )
    note = f"Added one general rule to {prompts[0].stem}: answer with the source's exact steps, in order."
    (work / "self_improve_input" / "CHANGES.md").write_text(note, encoding="utf-8")
    return note


class FakeAgentRunner:
    """Stands in for Opus in tests and with LLM_FAKE=1: a scripted edit, no cost. Its report
    says it was the fake, so it is never mistaken for a real run."""

    def __init__(self, edit: Callable[[Path], str] | None = None) -> None:
        self.edit = edit or fake_prompt_fix
        self.rounds = 0

    async def run_round(
        self, work: Path, brief: str, *, budget_usd: Decimal, api_key: str
    ) -> AgentRound:
        self.rounds += 1
        summary = self.edit(work)
        return AgentRound(
            summary=f"(fake agent, no model call) {summary}",
            cost_usd=Decimal(0),
            tool_calls=[{"tool": "Edit", "input": "self_improve_input/prompts", "denied": None}],
        )


def _short(tool_input: dict[str, Any]) -> str:
    text = " ".join(
        f"{k}={v}"
        for k, v in tool_input.items()
        if k in ("file_path", "path", "pattern", "command")
    )
    return text[:300]
