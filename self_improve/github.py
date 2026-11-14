"""Pull requests on GitHub, when GITHUB_TOKEN and GITHUB_REPO ("owner/name") are set (D-8).

A fine-grained token with "contents" and "pull requests" on that one repo is enough. Without
them, the loop keeps its branch in the local repo and approve/undo merge and revert locally.
The token goes in an HTTP header for one git command; it is never written to the repo config.
"""

import base64
from pathlib import Path
from typing import Any

import httpx

from core.config import get_settings
from self_improve.repo import git

API = "https://api.github.com"


def configured() -> bool:
    settings = get_settings()
    return bool(settings.github_token.get_secret_value() and settings.github_repo)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_settings().github_token.get_secret_value()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _auth() -> str:
    basic = base64.b64encode(
        f"x-access-token:{get_settings().github_token.get_secret_value()}".encode()
    ).decode()
    return f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"


def _url() -> str:
    return f"https://github.com/{get_settings().github_repo}.git"


async def push(repo: Path, branch: str) -> None:
    await git(repo, "-c", _auth(), "push", _url(), f"{branch}:{branch}")


async def fetch(repo: Path, ref: str) -> str:
    await git(repo, "-c", _auth(), "fetch", _url(), ref)
    return await git(repo, "rev-parse", "FETCH_HEAD")


async def _call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=API, headers=_headers(), timeout=30) as http:
        response = await http.request(method, path, json=body)
    response.raise_for_status()
    data: dict[str, Any] = response.json() if response.content else {}
    return data


async def open_pr(branch: str, base: str, title: str, body: str) -> tuple[int, str]:
    pr = await _call(
        "POST",
        f"/repos/{get_settings().github_repo}/pulls",
        {"title": title, "head": branch, "base": base, "body": body},
    )
    return int(pr["number"]), str(pr["html_url"])


async def merge_pr(number: int, title: str) -> str:
    merged = await _call(
        "PUT",
        f"/repos/{get_settings().github_repo}/pulls/{number}/merge",
        {"merge_method": "merge", "commit_title": title},
    )
    return str(merged.get("sha", ""))


async def close_pr(number: int) -> None:
    await _call("PATCH", f"/repos/{get_settings().github_repo}/pulls/{number}", {"state": "closed"})
