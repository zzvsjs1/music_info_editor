import pytest

from metadata_polisher.providers.cache import MemoryCache


def test_cache_miss_and_hit() -> None:
    cache = MemoryCache[str, tuple[str, ...]](capacity=2)

    assert cache.get("missing") is None

    cache.put("query:a", ("release-a",))

    assert cache.get("query:a") == ("release-a",)
    assert len(cache) == 1


def test_cache_hit_refreshes_recency_before_bounded_eviction() -> None:
    cache = MemoryCache[str, str](capacity=2)
    cache.put("oldest", "A")
    cache.put("newest", "B")

    # Reading A makes B the least recently used entry even though B was added
    # later. The next insertion must therefore evict B rather than A.
    assert cache.get("oldest") == "A"

    cache.put("replacement", "C")

    assert cache.get("newest") is None
    assert cache.get("oldest") == "A"
    assert cache.get("replacement") == "C"


def test_cache_update_replaces_value_and_refreshes_recency() -> None:
    cache = MemoryCache[str, int](capacity=2)
    cache.put("a", 1)
    cache.put("b", 2)

    cache.put("a", 10)
    cache.put("c", 3)

    assert cache.get("a") == 10
    assert cache.get("b") is None
    assert cache.get("c") == 3
    assert len(cache) == 2


def test_cache_clear_removes_all_entries_without_persistence_api() -> None:
    cache = MemoryCache[str, str](capacity=2)
    cache.put("a", "A")
    cache.put("b", "B")

    cache.clear()

    assert len(cache) == 0
    assert cache.get("a") is None
    assert cache.get("b") is None
    assert not hasattr(cache, "path")
    assert not hasattr(cache, "save")
    assert not hasattr(cache, "load")


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5])
def test_cache_requires_a_positive_integer_capacity(capacity: object) -> None:
    with pytest.raises((TypeError, ValueError), match="capacity"):
        MemoryCache[object, object](capacity=capacity)  # type: ignore[arg-type]
