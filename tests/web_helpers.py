"""A signed-in test browser for the web app, and a job queue that only records."""

import re
import uuid
from typing import Any

import httpx

from web.main import app
from web.state import set_queue


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(
        self, task: str, *, queue: str, lock: str | None = None, **kwargs: Any
    ) -> int:
        self.jobs.append((task, kwargs))
        return len(self.jobs)


class Browser:
    def __init__(self) -> None:
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
        )
        self.csrf = ""
        self.queue = RecordingQueue()
        set_queue(self.queue)

    async def signup(self, business: str = "Test Biz") -> str:
        email = f"{uuid.uuid4().hex[:10]}@test.local"
        response = await self.http.post(
            "/signup",
            data={
                "name": "Tess",
                "email": email,
                "password": "password123",
                "business_name": business,
            },
        )
        assert response.status_code == 303, response.text
        await self.refresh_csrf()
        return email

    async def refresh_csrf(self) -> None:
        page = await self.http.get("/settings/business")
        match = re.search(r'"X-CSRF-Token": "([^"]+)"', page.text)
        assert match, page.text[:500]
        self.csrf = match.group(1)

    async def get(self, path: str) -> httpx.Response:
        return await self.http.get(path)

    async def post(
        self, path: str, data: dict[str, Any] | None = None, *, htmx: bool = False
    ) -> httpx.Response:
        headers = {"X-CSRF-Token": self.csrf}
        if htmx:
            headers["HX-Request"] = "true"
        return await self.http.post(path, data=data or {}, headers=headers)

    async def close(self) -> None:
        await self.http.aclose()
