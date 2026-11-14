"""The HTTP client every crawler request uses: who we are, timeouts, and a size cap."""

import httpx

from core.config import get_settings

# sitemaps.org caps a sitemap at 50 MB uncompressed; help pages are far smaller
MAX_BODY_BYTES = 50 * 1024 * 1024


class BodyTooLarge(Exception):
    """The response kept going past the size cap."""


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": get_settings().crawler_user_agent},
        timeout=httpx.Timeout(20.0),  # seconds to connect, and between bytes
        follow_redirects=False,  # the fetcher decides: 301 is recorded, 302 is followed
        transport=transport,
    )


async def read_limited(response: httpx.Response, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    """Read a streamed body, giving up past max_bytes, so a huge or endless response can't
    fill the memory."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise BodyTooLarge(f"The response is larger than {max_bytes:,} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
