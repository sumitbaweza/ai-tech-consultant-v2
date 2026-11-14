"""How the web app talks to the Knowledge Brain: HTTP, with the business's own API key.

The Ask and Teach Chat, the Eval Lab, Go Live and the Knowledge screens use this client and
nothing else to reach the Brain's data (Task 2.8). The key decides the business on the Brain's
side, so a request can never reach another business's knowledge.
"""

import uuid
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel

from core.brain_api import (
    AskRequest,
    AskResponse,
    ContextRequest,
    ContextResponse,
    ExpertTaskIn,
    SearchRequest,
    SearchResponse,
    SourceView,
)
from core.config import get_settings
from core.crypto import decrypt
from core.db import tenant_session
from core.models import Business

# Tests route the web app's Brain calls straight into the Brain's ASGI app (no network)
_test_transport: httpx.AsyncBaseTransport | None = None


def use_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _test_transport
    _test_transport = transport


class BrainError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"Knowledge Brain answered {status}: {detail}")
        self.status = status
        self.detail = detail


class BrainClient:
    def __init__(
        self,
        api_key: str,
        *,
        user_id: uuid.UUID | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"}
        if user_id is not None:
            headers["X-Acting-User"] = str(user_id)  # recorded on what the person creates
        self._http = httpx.AsyncClient(
            base_url=base_url or get_settings().brain_api_url,
            headers=headers,
            timeout=httpx.Timeout(120, connect=5),
            transport=transport,
        )

    @classmethod
    async def for_business(
        cls, business_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> BrainClient:
        """The client for one business, with the web app's own key for it."""
        async with tenant_session(business_id) as session:
            business = await session.get(Business, business_id)
            if business is None or business.internal_key_enc is None:
                raise BrainError(404, "business has no Brain key")
            key = decrypt(business.internal_key_enc, business_id=business_id)
        return cls(key, user_id=user_id, transport=_test_transport)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ plain JSON calls

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        data: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(
                method, path, json=json, params=params, files=files, data=data
            )
        except httpx.HTTPError as exc:
            raise BrainError(503, f"the Knowledge Brain is not reachable ({exc})") from exc
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text
            raise BrainError(response.status_code, detail[:500])
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get_raw(self, path: str) -> httpx.Response:
        """A file (a stored screenshot) as it comes, for the web app to pass on."""
        try:
            return await self._http.get(path)
        except httpx.HTTPError as exc:
            raise BrainError(503, f"the Knowledge Brain is not reachable ({exc})") from exc

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self.request("GET", path, params=clean)

    async def post(self, path: str, body: BaseModel | dict[str, Any] | None = None) -> Any:
        payload = body.model_dump(mode="json") if isinstance(body, BaseModel) else body
        return await self.request("POST", path, json=payload)

    async def patch(self, path: str, body: dict[str, Any]) -> Any:
        return await self.request("PATCH", path, json=body)

    async def put(self, path: str, body: dict[str, Any]) -> Any:
        return await self.request("PUT", path, json=body)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)

    # ------------------------------------------------------------ typed calls

    async def search(self, request: SearchRequest) -> SearchResponse:
        return SearchResponse.model_validate(await self.post("/v1/search", request))

    async def context(self, request: ContextRequest) -> ContextResponse:
        return ContextResponse.model_validate(await self.post("/v1/context", request))

    async def ask(self, request: AskRequest) -> AskResponse:
        return AskResponse.model_validate(await self.post("/v1/ask", request))

    async def source(self, chunk_id: int) -> SourceView:
        return SourceView.model_validate(await self.get(f"/v1/chunks/{chunk_id}"))

    async def create_expert_task(self, task: ExpertTaskIn) -> dict[str, Any]:
        result = await self.post("/v1/expert-tasks", task)
        assert isinstance(result, dict)
        return result  # pyright: ignore[reportUnknownVariableType]
