from __future__ import annotations

import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable

import requests

from thoth_terminal.integrations.ibkr.config import IbkrConfig
from thoth_terminal.integrations.ibkr.rate_limit import EndpointRateLimiter


RequestLogger = Callable[
    [str, str, int | None, float | None, bool, str | None],
    None,
]


class IbkrClientError(RuntimeError):
    pass


class IbkrAuthError(IbkrClientError):
    pass


@dataclass(frozen=True)
class IbkrResponse:
    endpoint: str
    method: str
    data: Any
    status_code: int | None
    duration_ms: float | None
    cache_hit: bool = False


@dataclass
class _CacheEntry:
    expires_at: float
    response: IbkrResponse


class TTLCache:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = RLock()
        self._items: dict[str, _CacheEntry] = {}

    def get(self, key: str) -> IbkrResponse | None:
        now = self._clock()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            if entry.expires_at < now:
                self._items.pop(key, None)
                return None
            return IbkrResponse(
                endpoint=entry.response.endpoint,
                method=entry.response.method,
                data=entry.response.data,
                status_code=entry.response.status_code,
                duration_ms=entry.response.duration_ms,
                cache_hit=True,
            )

    def set(self, key: str, response: IbkrResponse, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            self._items[key] = _CacheEntry(expires_at=self._clock() + ttl_s, response=response)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class IbkrClient:
    def __init__(
        self,
        config: IbkrConfig,
        *,
        session: requests.Session | None = None,
        limiter: EndpointRateLimiter | None = None,
        cache: TTLCache | None = None,
        clock=time.monotonic,
        sleeper=time.sleep,
        request_logger: RequestLogger | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.limiter = limiter or EndpointRateLimiter(clock=clock, sleeper=sleeper)
        self.cache = cache or TTLCache(clock=clock)
        self._clock = clock
        self._sleeper = sleeper
        self._request_logger = request_logger
        self._auth_backoff_until = 0.0
        self._session_lock = RLock()
        self._session_generation = 0

    @property
    def auth_backoff_active(self) -> bool:
        return self._clock() < self._auth_backoff_until

    def clear_auth_backoff(self) -> None:
        with self._session_lock:
            self._auth_backoff_until = 0.0

    def refresh_session(self) -> None:
        """Invalidate cached and in-flight reads from the preceding session."""
        with self._session_lock:
            self._session_generation += 1
            self.cache.clear()
            self._auth_backoff_until = 0.0

    def get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        ttl_s: float = 0.0,
        attempts: int = 3,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request("GET", endpoint, params=params, ttl_s=ttl_s, attempts=attempts, timeout_s=timeout_s).data

    def post_read(
        self,
        endpoint: str,
        *,
        json_payload: dict[str, Any] | None = None,
        ttl_s: float = 0.0,
        attempts: int = 3,
        timeout_s: float | None = None,
    ) -> Any:
        return self.request(
            "POST",
            endpoint,
            json_payload=json_payload,
            ttl_s=ttl_s,
            attempts=attempts,
            timeout_s=timeout_s,
        ).data

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        ttl_s: float = 0.0,
        cache_key: str | None = None,
        attempts: int = 3,
        timeout_s: float | None = None,
    ) -> IbkrResponse:
        endpoint = "/" + str(endpoint or "").strip().lstrip("/")
        method = str(method or "GET").upper()
        key = cache_key or self._cache_key(method, endpoint, params=params, json_payload=json_payload)
        with self._session_lock:
            generation = self._session_generation
            if ttl_s > 0:
                cached = self.cache.get(key)
                if cached is not None:
                    self._log(endpoint, method, cached.status_code, cached.duration_ms, True, None)
                    return cached
            if self.auth_backoff_active and endpoint not in {"/sso/validate", "/iserver/auth/status", "/tickle"}:
                raise IbkrAuthError("IBKR session is not authenticated; polling is paused.")

        url = f"{self.config.gateway_base_url}{endpoint}"
        last_error: Exception | None = None
        max_attempts = max(1, int(attempts or 1))
        for attempt in range(max_attempts):
            status_code = None
            duration_ms = None
            started = self._clock()
            try:
                with self.limiter.acquire(endpoint):
                    response = self.session.request(
                        method,
                        url,
                        params=params,
                        json=json_payload,
                        timeout=float(timeout_s if timeout_s is not None else self.config.timeout_s),
                        verify=self.config.verify_ssl,
                    )
                duration_ms = max(0.0, (self._clock() - started) * 1000.0)
                status_code = int(response.status_code)
                if status_code in {401, 403}:
                    with self._session_lock:
                        if generation == self._session_generation:
                            self._mark_auth_failed()
                    raise IbkrAuthError("IBKR authentication is required.")
                if status_code == 429 and attempt < max_attempts - 1:
                    self._sleeper(min(8.0, 2.0**attempt))
                    continue
                response.raise_for_status()
                data = response.json() if response.content else {}
                out = IbkrResponse(
                    endpoint=endpoint,
                    method=method,
                    data=data,
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
                with self._session_lock:
                    if generation != self._session_generation:
                        raise IbkrAuthError("IBKR session changed while reading; refresh this request.")
                    self.cache.set(key, out, ttl_s)
                self._log(endpoint, method, status_code, duration_ms, False, None)
                return out
            except IbkrAuthError as exc:
                self._log(endpoint, method, status_code, duration_ms, False, str(exc))
                raise
            except Exception as exc:
                last_error = exc
                self._log(endpoint, method, status_code, duration_ms, False, str(exc)[:240])
                if attempt < max_attempts - 1:
                    self._sleeper(min(8.0, 2.0**attempt))

        raise IbkrClientError(str(last_error or "IBKR request failed"))

    def _mark_auth_failed(self) -> None:
        self._auth_backoff_until = self._clock() + max(1.0, float(self.config.auth_backoff_s))

    def _log(
        self,
        endpoint: str,
        method: str,
        status_code: int | None,
        duration_ms: float | None,
        cache_hit: bool,
        error_summary: str | None,
    ) -> None:
        if self._request_logger is not None:
            self._request_logger(endpoint, method, status_code, duration_ms, cache_hit, error_summary)

    @staticmethod
    def _cache_key(
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None,
        json_payload: dict[str, Any] | None,
    ) -> str:
        return f"{method}:{endpoint}:{repr(sorted((params or {}).items()))}:{repr(json_payload or {})}"
