from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class EndpointLimit:
    min_interval_s: float = 0.0
    max_concurrent: int = 1


DEFAULT_LIMITS: dict[str, EndpointLimit] = {
    "/fyi/unreadnumber": EndpointLimit(1.0),
    "/fyi/settings": EndpointLimit(1.0),
    "/fyi/disclaimer": EndpointLimit(1.0),
    "/fyi/deliveryoptions": EndpointLimit(1.0),
    "/fyi/notifications": EndpointLimit(1.0),
    "/portfolio/accounts": EndpointLimit(5.0),
    "/portfolio/subaccounts": EndpointLimit(5.0),
    "/portfolio/positions": EndpointLimit(5.0),
    "/portfolio/summary": EndpointLimit(5.0),
    "/iserver/account/pnl/partitioned": EndpointLimit(5.0),
    "/iserver/account/trades": EndpointLimit(5.0),
    "/iserver/account/orders": EndpointLimit(5.0),
    "/iserver/secdef/search": EndpointLimit(1.0),
    "/iserver/watchlists": EndpointLimit(5.0),
    "/iserver/watchlist": EndpointLimit(5.0),
    "/pa/performance": EndpointLimit(15.0 * 60.0),
    "/pa/allperiods": EndpointLimit(15.0 * 60.0),
    "/pa/summary": EndpointLimit(15.0 * 60.0),
    "/pa/transactions": EndpointLimit(15.0 * 60.0),
    "/sso/validate": EndpointLimit(5.0),
    "/tickle": EndpointLimit(1.0),
    "/iserver/marketdata/snapshot": EndpointLimit(0.10, max_concurrent=10),
    "/iserver/marketdata/history": EndpointLimit(0.0, max_concurrent=5),
}


class EndpointRateLimiter:
    def __init__(
        self,
        limits: dict[str, EndpointLimit] | None = None,
        *,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._limits = dict(limits or DEFAULT_LIMITS)
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._lock = threading.RLock()
        self._last_at: dict[str, float] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}

    def key_for(self, endpoint: str) -> str:
        path = "/" + str(endpoint or "").split("?", 1)[0].strip().lstrip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "portfolio" and parts[2] == "positions":
            return "/portfolio/positions"
        if len(parts) >= 3 and parts[0] == "portfolio" and parts[2] == "summary":
            return "/portfolio/summary"
        if path.startswith("/fyi/disclaimer/"):
            return "/fyi/disclaimer"
        if path.startswith("/fyi/settings/"):
            return "/fyi/settings"
        if path.startswith("/fyi/deliveryoptions/"):
            return "/fyi/deliveryoptions"
        if path.startswith("/fyi/notifications/"):
            return "/fyi/notifications"
        if path.startswith("/iserver/watchlist"):
            return "/iserver/watchlist" if path == "/iserver/watchlist" else "/iserver/watchlists"
        return path

    def limit_for(self, endpoint: str) -> EndpointLimit:
        return self._limits.get(self.key_for(endpoint), EndpointLimit(0.0, max_concurrent=1))

    def wait(self, endpoint: str) -> None:
        key = self.key_for(endpoint)
        limit = self.limit_for(endpoint)
        if limit.min_interval_s <= 0:
            return
        with self._lock:
            now = self._clock()
            last = self._last_at.get(key)
            wait_s = 0.0 if last is None else (last + limit.min_interval_s) - now
            if wait_s > 0:
                self._sleeper(wait_s)
                now = self._clock()
            self._last_at[key] = now

    def _semaphore_for(self, endpoint: str) -> threading.BoundedSemaphore:
        key = self.key_for(endpoint)
        limit = self.limit_for(endpoint)
        with self._lock:
            sem = self._semaphores.get(key)
            if sem is None:
                sem = threading.BoundedSemaphore(max(1, int(limit.max_concurrent or 1)))
                self._semaphores[key] = sem
            return sem

    @contextmanager
    def acquire(self, endpoint: str) -> Iterator[None]:
        sem = self._semaphore_for(endpoint)
        sem.acquire()
        try:
            self.wait(endpoint)
            yield
        finally:
            sem.release()
