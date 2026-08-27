from __future__ import annotations

from uuid import UUID

from voice_realtime.meeting.inner_os.cache import BoundedTTLCache


def test_cache_expires_entries_and_evicts_oldest_when_bounded() -> None:
    cache: BoundedTTLCache[str, dict[str, str]] = BoundedTTLCache(
        ttl_secs=1, max_entries=2, max_bytes=10_000
    )
    cache.put("a", {"value": "one"})
    cache.put("b", {"value": "two"})
    cache.put("c", {"value": "three"})

    assert cache.get("a") is None
    assert cache.get("b") == {"value": "two"}
    assert cache.get("c") == {"value": "three"}


def test_cache_enforces_serialized_byte_limit_and_pop_is_explicit() -> None:
    cache: BoundedTTLCache[UUID, dict[str, str]] = BoundedTTLCache(
        ttl_secs=60, max_entries=8, max_bytes=100
    )
    key = UUID(int=1)
    cache.put(key, {"value": "x" * 1_000})
    assert cache.get(key) is None
    cache.put(key, {"value": "ok"})
    assert cache.pop(key) == {"value": "ok"}
    assert cache.get(key) is None
