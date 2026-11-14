"""Jinja templates: the shared layout in web/templates, each feature's screens in its own
folder (workspace/templates, knowledge_brain/templates, ...)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

from core.config import REPO_ROOT, get_settings
from web.markdown import render_markdown

TEMPLATE_DIRS = [
    REPO_ROOT / "web" / "templates",
    REPO_ROOT / "workspace" / "templates",
    REPO_ROOT / "knowledge_brain" / "templates",
    REPO_ROOT / "ask_teach_chat" / "templates",
    REPO_ROOT / "eval_lab" / "templates",
    REPO_ROOT / "self_improve" / "templates",
    REPO_ROOT / "go_live" / "templates",
]

env = Environment(
    loader=ChoiceLoader([FileSystemLoader(str(path)) for path in TEMPLATE_DIRS]),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

NAV = [
    ("Dashboard", "/", "dashboard"),
    ("Go Live", "/setup", "setup"),
    ("Knowledge", "/knowledge", "knowledge"),
    ("Chat", "/chat", "chat"),
    ("Lessons", "/lessons", "lessons"),
    ("Expert tasks", "/experts", "experts"),
    ("Eval Lab", "/evals", "evals"),
    ("Self-Improve", "/improve", "improve"),
    ("Logs and costs", "/logs", "logs"),
    ("Settings", "/settings", "settings"),
]


def money(value: Decimal | float | int | None, places: int = 4) -> str:
    if value is None:
        return "-"
    return f"${float(value):,.{places}f}"


def _as_datetime(value: datetime | str | None) -> datetime | None:
    """Dates come as datetimes from the database and as ISO strings from the Brain's API."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def when(value: datetime | str | None) -> str:
    moment = _as_datetime(value)
    return moment.astimezone(UTC).strftime("%d %b %Y %H:%M") if moment else "-"


def ago(value: datetime | str | None) -> str:
    moment = _as_datetime(value)
    if moment is None:
        return "never"
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds // size} {unit} ago"
    return "just now"


def number(value: float | int | None) -> str:
    return "-" if value is None else f"{value:,}"


def percent(value: float | None, places: int = 0) -> str:
    return "-" if value is None else f"{value * 100:.{places}f}%"


env.filters.update(money=money, when=when, ago=ago, number=number, percent=percent)
globals_: dict[str, Any] = cast(dict[str, Any], env.globals)
globals_.update(markdown=render_markdown, NAV=NAV)


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    ctx = getattr(request.state, "ctx", None)
    page = env.get_template(template).render(
        request=request,
        ctx=ctx,
        csrf=ctx.csrf if ctx else "",
        fake_models=get_settings().llm_fake,
        path=request.url.path,
        **context,
    )
    return HTMLResponse(page, status_code=status_code)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"
