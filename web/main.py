"""The web service: every screen, and the APIs of Workspace, Chat, Eval Lab, Self-Improve and
Go Live. http://localhost:8080

It reaches the Knowledge Brain only through the Brain's HTTP API (core/brain_client.py).
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from ask_teach_chat import routes as chat_routes
from core.config import REPO_ROOT
from core.db import dispose_engines
from eval_lab import routes as eval_routes
from go_live import routes as setup_routes
from knowledge_brain import screens as knowledge_screens
from self_improve import routes as improve_routes
from web import dashboard
from web.deps import Redirect
from web.templating import is_htmx, render
from web.worker import app as jobs_app
from workspace import auth, editors, logs, settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Open the job queue's connection pool, so screens can start background jobs
    async with jobs_app.open_async():
        yield
    await dispose_engines()


app = FastAPI(title="AI Tech Consultant", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=REPO_ROOT / "web" / "static"), name="static")


@app.exception_handler(Redirect)
async def redirect_handler(request: Request, exc: Redirect) -> Response:
    if is_htmx(request):  # an HTMX request follows a redirect only through this header
        return Response(status_code=204, headers={"HX-Redirect": exc.url})
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> Response:
    message = str(exc.detail)
    if is_htmx(request):
        return HTMLResponse(f'<div class="notice bad">{message}</div>', status_code=200)
    return render(
        request,
        "error.html",
        status_code=exc.status_code,
        message=message,
        heading="Not found" if exc.status_code == 404 else "That didn't work",
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


for router in (
    auth.router,
    settings.router,
    settings.oauth_router,
    editors.router,
    logs.router,
    dashboard.router,
    knowledge_screens.router,
    chat_routes.router,
    eval_routes.router,
    improve_routes.router,
    setup_routes.router,
):
    app.include_router(router)
