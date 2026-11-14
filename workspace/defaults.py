"""What every new business starts with: the 3 roles, the routing table, the model per step.

The role texts are files in prompts/_roles/, like every other prompt (Task 1.3: no prompt
hidden in code).
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast

import yaml

from core.config import REPO_ROOT

ROLES_DIR = REPO_ROOT / "prompts" / "_roles"

# Angry, refund and legal questions always go to a human, whatever the role says (Task 3.6)
ALWAYS_HAND_OVER = ("angry", "refund", "legal")

CAPABILITIES = {
    "answer_from_brain": "Answer from the Knowledge Brain",
    "draft_reply": "Write a reply the human can send",
    "create_expert_task": "Create an expert task",
    "share_price": "Share a price",
}

# The routing table before the Eval Lab has measured anything (Settings > Routing).
# Simple how-to questions start on Haiku; a failed quick check moves the answer up to Sonnet.
DEFAULT_ROUTING: dict[tuple[str, str], str] = {
    ("write_answer", "low"): "claude-haiku-5-5",
    ("write_answer", "medium"): "claude-sonnet-5-5",
    ("write_answer", "high"): "claude-sonnet-5-5",
    ("tag_job", "low"): "claude-haiku-5-5",
    ("find_facts", "low"): "claude-haiku-5-5",
}


@dataclass(frozen=True)
class DefaultRole:
    key: str
    name: str
    goal: str
    tone: str
    prompt: str
    capabilities: tuple[str, ...]
    knowledge_types: tuple[str, ...]
    handover_note: str


def _parse_role(path: Path) -> DefaultRole:
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.DOTALL)
    if not match:
        raise ValueError(f"{path} has no header")
    header = cast(dict[str, Any], yaml.safe_load(match.group(1)) or {})
    return DefaultRole(
        key=path.stem.replace("_", "-"),
        name=str(header["name"]),
        goal=str(header["goal"]),
        tone=str(header["tone"]),
        prompt=match.group(2).strip() + "\n",
        capabilities=tuple(header.get("capabilities", [])),
        knowledge_types=tuple(header.get("knowledge_types", [])),
        handover_note=str(header.get("handover", "")),
    )


@lru_cache
def default_roles() -> list[DefaultRole]:
    order = ["support-engineer", "support-executive", "ai-consultant"]
    roles = [_parse_role(path) for path in sorted(ROLES_DIR.glob("*.md"))]
    return sorted(roles, key=lambda r: order.index(r.key) if r.key in order else 99)
