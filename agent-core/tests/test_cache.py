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


def test_an_entry_expires_exactly_at_its_deadline() -> None:
    now = [0.0]
    cache: TtlCache[str] = TtlCache(ttl_s=5, max_entries=3, clock=lambda: now[0])
    cache.put("a", "1")
    now[0] = 4.5
    assert cache.has("a")
    now[0] = 5
    assert not cache.has("a")


def test_a_per_entry_ttl_replaces_the_default() -> None:
    now = [0.0]
    cache: TtlCache[str] = TtlCache(ttl_s=100, max_entries=3, clock=lambda: now[0])
    cache.put("short", "1", ttl_s=2)
    cache.put("default", "2")
    now[0] = 1
    assert cache.has("short")
    now[0] = 2
    assert not cache.has("short") and cache.has("default")


def test_put_purges_an_expired_entry_that_is_not_the_oldest() -> None:
    now = [0.0]
    cache: TtlCache[str] = TtlCache(ttl_s=100, max_entries=2, clock=lambda: now[0])
    cache.put("oldest", "1")
    cache.put("expired", "2", ttl_s=1)
    now[0] = 2
    cache.put("new", "3")  # without the purge, the bound would evict "oldest", not "expired"
    assert cache.has("oldest") and cache.has("new")
