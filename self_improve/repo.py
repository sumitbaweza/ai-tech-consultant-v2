"""Git for the Self-Improve Loop. Opus never works in the repo itself.

1. export(): the base commit is exported (git archive) into a fresh folder outside the repo,
   without the hidden check set, and committed there as "base". Opus edits only that folder,
   so it can't reach main, the repo's history, or the hidden set.
2. After each round, protect() puts back every protected file Opus changed and reports it.
3. When the run ends, to_branch() applies Opus's diff as one commit on a new branch
   improve/<date>-<id>, through a temporary git worktree. Main is never touched.
4. merge() and revert() are the human's one-click approve and undo.
"""

import asyncio
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

# Never changed by Opus: the test set, the tests, the judge and its prompts, the rules that
# score, this loop itself, the database schema and the dependencies
PROTECTED = (
    "benchmark/",
    "tests/",
    "prompts/eval.judge",
    "eval_lab/judge.py",
    "eval_lab/rules.py",
    "self_improve/",
    "migrations/",
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "docker-compose.yml",
    ".github/",
)
HIDDEN = "benchmark/hidden/"  # never exported: Opus can't see the hidden check set
INPUT_DIR = "self_improve_input"  # the business's prompts, copied in for Opus to edit
IDENTITY = (
    "-c",
    "user.name=Self-Improve (Claude Opus 5.5)",
    "-c",
    "user.email=self-improve@localhost",
)


class GitError(RuntimeError):
    pass


@dataclass
class Done:
    code: int
    out: bytes
    err: bytes

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace").strip()


async def run(
    args: list[str],
    cwd: Path,
    *,
    stdin: bytes | None = None,
    limit_seconds: float = 600,
    env: dict[str, str] | None = None,
) -> Done:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), limit_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        return Done(
            124, b"", f"{args[0]} took longer than {limit_seconds:.0f} s and was stopped".encode()
        )
    return Done(process.returncode or 0, out, err)


async def git(cwd: Path, *args: str, check: bool = True, stdin: bytes | None = None) -> str:
    # safe.directory: the repo is mounted from the host, so its owner differs from ours
    done = await run(["git", "-c", "safe.directory=*", *args], cwd, stdin=stdin)
    if check and done.code != 0:
        raise GitError(
            f"git {' '.join(args[:3])} failed: {done.err.decode('utf-8', 'replace').strip()[:500]}"
        )
    return done.text


def is_protected(path: str) -> bool:
    return any(path == rule.rstrip("/") or path.startswith(rule) for rule in PROTECTED)


async def head(repo: Path) -> str:
    return await git(repo, "rev-parse", "HEAD")


async def current_branch(repo: Path) -> str:
    return await git(repo, "rev-parse", "--abbrev-ref", "HEAD")


async def export(repo: Path, base_sha: str, work: Path) -> None:
    """The base commit as a fresh repo in `work`, without the hidden set or any history."""
    archive = await run(
        ["git", "-c", "safe.directory=*", "archive", "--format=tar", base_sha], repo
    )
    if archive.code != 0:
        raise GitError(f"git archive failed: {archive.err.decode('utf-8', 'replace')[:300]}")
    work.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(archive.out)) as tar:
        members = [m for m in tar.getmembers() if not m.name.startswith(HIDDEN.rstrip("/"))]
        tar.extractall(work, members=members, filter="data")  # "data": no absolute paths or ..
    await git(work, "init", "-q", "-b", "work")
    exclude = work / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(
        f"{INPUT_DIR}/\n__pycache__/\n.pytest_cache/\n.ruff_cache/\n", encoding="utf-8"
    )
    await git(work, "add", "-A")
    await git(work, *IDENTITY, "commit", "-q", "-m", f"base {base_sha[:12]}")
    await git(work, "tag", "base")


async def changed_files(work: Path) -> list[str]:
    await git(work, "add", "-A")
    listing = await git(work, "diff", "--cached", "--name-only", "base")
    return [line for line in listing.splitlines() if line]


async def protect(work: Path) -> list[str]:
    """Put back every protected file Opus changed, added or deleted. Returns those paths."""
    broken = [path for path in await changed_files(work) if is_protected(path)]
    for path in broken:
        in_base = (await run(["git", "cat-file", "-e", f"base:{path}"], work)).code == 0
        if in_base:
            await git(work, "checkout", "base", "--", path)
        else:
            await git(work, "rm", "-q", "-f", "--cached", "--", path)
            (work / path).unlink(missing_ok=True)
    return broken


async def patch(work: Path) -> str:
    await git(work, "add", "-A")
    return await git(work, "diff", "--cached", "--binary", "base") + "\n"


def hardcoded_questions(diff_text: str, questions: list[str]) -> list[str]:
    """Anti-cheat: a fix that copies a test question into the code or a prompt is special-casing
    the test. Returns the questions found in the added lines (long ones only, to avoid noise)."""
    added = "\n".join(
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    lowered = " ".join(added.lower().split())
    return [
        q for q in questions if len(q) >= 25 and " ".join(q.lower().split()).rstrip("?") in lowered
    ]


async def to_branch(
    repo: Path, base_sha: str, branch: str, diff_text: str, message: str, worktrees: Path
) -> str:
    """Opus's change as one commit on a new branch, made in a temporary worktree."""
    worktrees.mkdir(parents=True, exist_ok=True)
    tree = worktrees / branch.replace("/", "-")
    await git(repo, "worktree", "add", "-q", "-b", branch, str(tree), base_sha)
    try:
        if diff_text.strip():
            await git(tree, "apply", "--index", "--whitespace=nowarn", stdin=diff_text.encode())
        await git(tree, *IDENTITY, "commit", "-q", "--allow-empty", "-m", message)
        return await head(tree)
    finally:
        await git(repo, "worktree", "remove", "--force", str(tree), check=False)


async def branch_diff(repo: Path, base_sha: str, branch: str) -> str:
    return await git(repo, "diff", f"{base_sha}...{branch}", check=False)


async def merge(repo: Path, branch: str, *, into: str, message: str, who: str) -> str:
    """The human's approve: a merge commit on `into` (the repo must be on that branch)."""
    on = await current_branch(repo)
    if on != into:
        raise GitError(
            f"The repo is on the branch {on}, not {into}. Switch to {into} and try again."
        )
    await git(
        repo,
        "-c",
        f"user.name={who}",
        "-c",
        "user.email=self-improve@localhost",
        "merge",
        "--no-ff",
        "-m",
        message,
        branch,
    )
    return await head(repo)


async def revert(repo: Path, merge_sha: str, *, who: str) -> str:
    """The human's undo: a new commit that takes the merge back out (history is kept)."""
    await git(
        repo,
        "-c",
        f"user.name={who}",
        "-c",
        "user.email=self-improve@localhost",
        "revert",
        "--no-edit",
        "-m",
        "1",
        merge_sha,
    )
    return await head(repo)
