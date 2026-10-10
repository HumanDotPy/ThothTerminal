from __future__ import annotations

from collections import OrderedDict
from itertools import islice
from threading import Event, RLock, get_ident
import time
from typing import Callable, Generic, TypeVar, cast

T = TypeVar("T")

_MISSING = object()
_MAX_WEIGHT_NODES = 512
_MAX_WEIGHT_DEPTH = 32
_WEIGHT_SAMPLE_WIDTH = 8
_OPAQUE_WEIGHT = 64


class _Flight(Generic[T]):
    def __init__(self) -> None:
        self.event = Event()
        self.owner_ident = get_ident()
        self.value: T | object = _MISSING
        self.exception: BaseException | None = None


def json_cache_weight(value: object) -> int:
    """Cheaply estimate a JSON-like payload's serialized size.

    This deliberately avoids building a second, throwaway JSON string on every
    cache insertion.  The estimate stays in the same rough byte scale as the
    former compact-JSON measurement. Large collections are sampled and their
    average element weight extrapolated; traversal is also node/depth bounded so
    adversarially deep, cyclic, or extremely wide values cannot turn cache
    accounting into the hot path.
    """

    nodes = 0
    seen_containers: set[int] = set()

    def estimate(item: object, depth: int) -> int:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_WEIGHT_NODES or depth > _MAX_WEIGHT_DEPTH:
            return _OPAQUE_WEIGHT

        if item is None:
            return 4
        if isinstance(item, bool):
            return 4 if item else 5
        if isinstance(item, str):
            # Quotes plus characters. Escaping is intentionally not scanned:
            # this is bounded cache accounting, not an encoded-length promise.
            return len(item) + 2
        if isinstance(item, bytes):
            return len(item) + 2
        if isinstance(item, (int, float)):
            # Avoid number-to-string conversion solely for cache accounting.
            return 24
        if isinstance(item, dict):
            identity = id(item)
            if identity in seen_containers:
                return 4
            seen_containers.add(identity)
            length = len(item)
            if not length:
                return 2
            sample = list(islice(item.items(), _WEIGHT_SAMPLE_WIDTH))
            sampled_weight = sum(
                estimate(key, depth + 1) + 1 + estimate(child, depth + 1)
                for key, child in sample
            )
            return 2 + max(0, length - 1) + (sampled_weight * length // len(sample))
        if isinstance(item, (list, tuple, set, frozenset)):
            identity = id(item)
            if identity in seen_containers:
                return 4
            seen_containers.add(identity)
            length = len(item)
            if not length:
                return 2
            if isinstance(item, (list, tuple)) and length > _WEIGHT_SAMPLE_WIDTH:
                # Sample across the collection instead of biasing toward its
                # first rows while retaining O(sample-width) work.
                indexes = [
                    index * (length - 1) // (_WEIGHT_SAMPLE_WIDTH - 1)
                    for index in range(_WEIGHT_SAMPLE_WIDTH)
                ]
                sample = [item[index] for index in indexes]
            else:
                sample = list(islice(item, _WEIGHT_SAMPLE_WIDTH))
            sampled_weight = sum(estimate(child, depth + 1) for child in sample)
            return 2 + max(0, length - 1) + (sampled_weight * length // len(sample))

        # Do not invoke arbitrary __str__, __repr__, or __sizeof__ methods.
        # Uncommon opaque values get a predictable nominal charge.
        return _OPAQUE_WEIGHT

    return estimate(value, 0)


class TTLCache(Generic[T]):
    def __init__(
        self,
        *,
        max_entries: int = 256,
        default_ttl_s: float = 60.0,
        max_weight: int | None = None,
        get_weight: Callable[[T], int] | None = None,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.default_ttl_s = max(0.0, float(default_ttl_s))
        self.max_weight = max(1, int(max_weight)) if max_weight is not None else None
        self._get_weight = get_weight
        self._total_weight = 0
        self._lock = RLock()
        self._items: OrderedDict[str, tuple[float, T, int]] = OrderedDict()
        self._flights: dict[str, _Flight[T]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def total_weight(self) -> int:
        with self._lock:
            return self._total_weight

    def _weight_for(self, value: T) -> int:
        if self._get_weight is None:
            return 1
        return max(0, int(self._get_weight(value)))

    def _pop(self, key: str) -> tuple[float, T, int] | None:
        entry = self._items.pop(key, None)
        if entry is not None:
            self._total_weight = max(0, self._total_weight - int(entry[2]))
        return entry

    def _evict_until_within_limits(self) -> None:
        while len(self._items) > self.max_entries:
            _key, (_expires_at, _value, weight) = self._items.popitem(last=False)
            self._total_weight = max(0, self._total_weight - int(weight))
        while self.max_weight is not None and self._total_weight > self.max_weight and self._items:
            _key, (_expires_at, _value, weight) = self._items.popitem(last=False)
            self._total_weight = max(0, self._total_weight - int(weight))

    def get(self, key: str) -> T | None:
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            expires_at, value, _weight = entry
            if expires_at <= now:
                self._pop(key)
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: str, value: T, *, ttl_s: float | None = None) -> T:
        ttl = self.default_ttl_s if ttl_s is None else max(0.0, float(ttl_s))
        weight = self._weight_for(value)
        with self._lock:
            self._pop(key)
            if self.max_weight is not None and weight > self.max_weight:
                return value
            self._items[key] = (time.monotonic() + ttl, value, weight)
            self._total_weight += weight
            self._items.move_to_end(key)
            self._evict_until_within_limits()
            return value

    def get_or_set(self, key: str, factory: Callable[[], T], *, ttl_s: float | None = None) -> T:
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is not None:
                expires_at, value, _weight = entry
                if expires_at <= now:
                    self._pop(key)
                else:
                    self._items.move_to_end(key)
                    return value
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight[T]()
                self._flights[key] = flight
                is_leader = True
            else:
                if flight.owner_ident == get_ident():
                    raise RuntimeError(f"recursive get_or_set for cache key {key!r}")
                is_leader = False

        if not is_leader:
            flight.event.wait()
            if flight.exception is not None:
                raise flight.exception
            return cast(T, flight.value)

        try:
            value = factory()
            self.set(key, value, ttl_s=ttl_s)
        except BaseException as exc:
            with self._lock:
                flight.exception = exc
                flight.event.set()
                if self._flights.get(key) is flight:
                    del self._flights[key]
            raise

        with self._lock:
            flight.value = value
            flight.event.set()
            if self._flights.get(key) is flight:
                del self._flights[key]
        return value

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._pop(key)

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [key for key in self._items if key.startswith(prefix)]
            for key in keys:
                self._pop(key)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._total_weight = 0

    def prune(self) -> int:
        now = time.monotonic()
        with self._lock:
            keys = [key for key, (expires_at, _value, _weight) in self._items.items() if expires_at <= now]
            for key in keys:
                self._pop(key)
            return len(keys)
