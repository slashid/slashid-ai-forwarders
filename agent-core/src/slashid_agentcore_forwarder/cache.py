"""A small in-memory TTL map. A resilient store can replace it behind the same three methods."""

from __future__ import annotations

import time
from collections.abc import Callable


class TtlCache[V]:
    def __init__(
        self, *, ttl_s: float, max_entries: int, clock: Callable[[], float] = time.time
    ) -> None:
        self._ttl_s = ttl_s
        self._max = max_entries
        self._clock = clock
        self._items: dict[str, tuple[float, V]] = {}

    def has(self, key: str) -> bool:
        entry = self._items.get(key)
        if entry is None:
            return False
        if entry[0] <= self._clock():
            del self._items[key]
            return False
        return True

    def get(self, key: str) -> V | None:
        return self._items[key][1] if self.has(key) else None

    def put(self, key: str, value: V, *, ttl_s: float | None = None) -> None:
        now = self._clock()
        self._items = {k: v for k, v in self._items.items() if v[0] > now}
        self._items.pop(key, None)
        self._items[key] = (now + (self._ttl_s if ttl_s is None else ttl_s), value)
        while len(self._items) > self._max:
            del self._items[next(iter(self._items))]

    def pop(self, key: str) -> V | None:
        value = self.get(key)
        self._items.pop(key, None)
        return value
