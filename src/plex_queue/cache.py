from __future__ import annotations

import threading
import time
from typing import Generic, TypeVar

T = TypeVar("T")


class TtlCache(Generic[T]):
    """Small process-local cache with a fixed time-to-live."""

    def __init__(self, ttl_seconds: float = 900):
        self.ttl = ttl_seconds
        self._items: dict[object, tuple[float, T]] = {}
        self._lock = threading.Lock()

    def get(self, key: object) -> T | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires, value = item
            if expires <= now:
                self._items.pop(key, None)
                return None
            return value

    def set(self, key: object, value: T) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
