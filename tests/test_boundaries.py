"""The Ask and Teach Chat and the Eval Lab must call the Knowledge Brain through its API, not copy
or import its code (the brief, "What you build"). Checked here on the source itself, so a
shortcut fails the build. The database enforces the same rule (test_isolation.py)."""

import ast
from pathlib import Path

from core.config import REPO_ROOT

WEB_SIDE = ("ask_teach_chat", "eval_lab", "self_improve", "go_live", "workspace", "web")
# Knowledge screens run in the web service; they use the Brain's HTTP API like everyone else
WEB_SIDE_FILES_IN_BRAIN = {"knowledge_brain/screens.py"}
# The one exception: scoring a Self-Improve branch must run that branch's Brain, so the scorer
# mounts the Brain's API app in-process and still talks to it only through HTTP requests. It
# may import those two app objects and nothing else of the Brain.
ALLOWED = {
    "self_improve/evaluate.py": {"knowledge_brain.api.main.app", "knowledge_brain.worker.app"},
}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def python_files(folder: str) -> list[Path]:
    return [p for p in (REPO_ROOT / folder).rglob("*.py") if "templates" not in p.parts]


def test_web_side_code_never_imports_the_brain() -> None:
    offenders: list[str] = []
    files = [p for folder in WEB_SIDE for p in python_files(folder)]
    files += [REPO_ROOT / name for name in WEB_SIDE_FILES_IN_BRAIN]
    for path in files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        for module in imports_of(path):
            if module in ALLOWED.get(relative, set()):
                continue
            if module.startswith("knowledge_brain") and module != "knowledge_brain.screens":
                offenders.append(f"{relative} imports {module}")
    assert offenders == []


def test_the_brain_never_imports_web_side_features() -> None:
    offenders: list[str] = []
    for path in python_files("knowledge_brain"):
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in WEB_SIDE_FILES_IN_BRAIN:
            continue
        for module in imports_of(path):
            if module.split(".")[0] in WEB_SIDE:
                offenders.append(f"{relative} imports {module}")
    assert offenders == []


def test_no_source_file_hides_control_characters() -> None:
    """A shell once turned a regex's word boundary into a real backspace character, and the rule
    silently never matched. Text files may hold tabs and newlines, nothing else invisible."""
    bad = set(range(32)) - {9, 10, 13}
    offenders: list[str] = []
    for pattern in ("**/*.py", "**/*.md", "**/*.yaml", "**/*.html", "**/*.toml"):
        for path in REPO_ROOT.glob(pattern):
            if ".venv" in path.parts or "node_modules" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(ord(ch) in bad for ch in text):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
