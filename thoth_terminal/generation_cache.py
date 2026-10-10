from __future__ import annotations

from threading import RLock
from typing import Callable, Generic, TypeVar

from thoth_terminal.ttl_cache import TTLCache

T = TypeVar("T")


class GenerationTTLCache(Generic[T]):
    """TTL cache with constant-time logical prefix invalidation.

    Prefix invalidation advances a generation rather than walking cached keys.
    Entries from older generations remain bounded by the underlying cache's
    normal entry/weight limits and are unreachable immediately.
    """

    def __init__(self, **kwargs: object) -> None:
        self._cache: TTLCache[T] = TTLCache(**kwargs)  # type: ignore[arg-type]
        self._generation_lock = RLock()
        self._generations: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._cache)

    @property
    def total_weight(self) -> int:
        return self._cache.total_weight

    def _versioned_key(self, key: str) -> str:
        with self._generation_lock:
            versions = tuple(
                (prefix, generation)
                for prefix, generation in self._generations.items()
                if key.startswith(prefix)
            )
        if not versions:
            return key
        stamp = "|".join(f"{len(prefix)}:{prefix}{generation}" for prefix, generation in sorted(versions))
        return f"__generation__:{stamp}:{key}"

    def get(self, key: str) -> T | None:
        return self._cache.get(self._versioned_key(key))

    def set(self, key: str, value: T, *, ttl_s: float | None = None) -> T:
        return self._cache.set(self._versioned_key(key), value, ttl_s=ttl_s)

    def get_or_set(self, key: str, factory: Callable[[], T], *, ttl_s: float | None = None) -> T:
        # Capture the generation before running the factory. If invalidation
        # races the load, the result is stored only under the old generation.
        versioned_key = self._versioned_key(key)
        return self._cache.get_or_set(versioned_key, factory, ttl_s=ttl_s)

    def invalidate(self, key: str) -> None:
        self._cache.invalidate(self._versioned_key(key))

    def invalidate_prefix(self, prefix: str) -> int:
        with self._generation_lock:
            self._generations[prefix] = self._generations.get(prefix, 0) + 1
        return 0

    def clear(self) -> None:
        with self._generation_lock:
            self._generations.clear()
            self._cache.clear()

    def prune(self) -> int:
        return self._cache.prune()
