"""Keeps requests to each host spaced out, across every task that shares the limiter."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit


class HostRateLimiter:
    """At most `requests_per_second` requests to each host (Task 2.3: don't overload the site).

    Each caller books the next free time slot for its host while holding the lock, then sleeps
    until that slot outside the lock. Two callers asking at once get slots one interval apart,
    and waiting for one host never holds up another host.
    """

    def __init__(
        self,
        requests_per_second: float,
        *,
        per_host: dict[str, float] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1 / requests_per_second
        # A faster rate for hosts built for it, such as an image CDN
        self._host_intervals = {host: 1 / rate for host, rate in (per_host or {}).items()}
        self._next_slot: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._sleep = sleep

    async def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        async with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot.get(host, now))
            self._next_slot[host] = slot + self._host_intervals.get(host, self._interval)
        if slot > now:
            await self._sleep(slot - now)
