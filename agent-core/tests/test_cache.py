from slashid_agentcore_forwarder.cache import TtlCache


def test_entries_expire_and_pop() -> None:
    now = [0.0]
    cache: TtlCache[str] = TtlCache(ttl_s=10, max_entries=3, clock=lambda: now[0])
    cache.put("a", "1")
    assert cache.get("a") == "1" and cache.has("a")
    now[0] = 11
    assert cache.get("a") is None and not cache.has("a")
    cache.put("b", "2")
    assert cache.pop("b") == "2" and cache.pop("b") is None


def test_a_per_entry_ttl_and_the_oldest_leaves_first() -> None:
    now = [0.0]
    cache: TtlCache[str] = TtlCache(ttl_s=10, max_entries=2, clock=lambda: now[0])
    cache.put("a", "1")
    cache.put("b", "2", ttl_s=1)
    cache.put("c", "3")
    assert not cache.has("a") and cache.has("b") and cache.has("c")
    now[0] = 2
    cache.put("d", "4")  # drops the expired b, then bounds
    assert not cache.has("b") and cache.has("c") and cache.has("d")
