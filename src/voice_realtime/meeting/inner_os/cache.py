from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _CacheEntry[ValueT]:
    value: ValueT
    expires_at: float
    size_bytes: int


class BoundedTTLCache[KeyT, ValueT]:
    """进程内、非持久化的 TTL/LRU 缓存，超过任一边界即淘汰。"""

    def __init__(self, *, ttl_secs: int, max_entries: int, max_bytes: int) -> None:
        if ttl_secs <= 0 or max_entries <= 0 or max_bytes <= 0:
            raise ValueError("cache limits must be positive")
        self._ttl_secs = ttl_secs
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._items: OrderedDict[KeyT, _CacheEntry[ValueT]] = OrderedDict()
        self._bytes = 0

    def put(self, key: KeyT, value: ValueT) -> bool:
        size = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
        self._remove_expired()
        if size > self._max_bytes:
            self.pop(key)
            return False
        self.pop(key)
        self._items[key] = _CacheEntry(value, time.monotonic() + self._ttl_secs, size)
        self._bytes += size
        self._trim()
        return key in self._items

    def get(self, key: KeyT) -> ValueT | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self.pop(key)
            return None
        self._items.move_to_end(key)
        return entry.value

    def pop(self, key: KeyT) -> ValueT | None:
        entry = self._items.pop(key, None)
        if entry is None:
            return None
        self._bytes -= entry.size_bytes
        return entry.value

    def __len__(self) -> int:
        self._remove_expired()
        return len(self._items)

    def _remove_expired(self) -> None:
        now = time.monotonic()
        for key, entry in tuple(self._items.items()):
            if entry.expires_at <= now:
                self.pop(key)

    def _trim(self) -> None:
        while len(self._items) > self._max_entries or self._bytes > self._max_bytes:
            self.pop(next(iter(self._items)))
