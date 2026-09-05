"""Small bounded memory-only cache for provider session data."""

from collections import OrderedDict


class MemoryCache[K, V]:
    """Bounded least-recently-used cache with no persistence boundary."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int:
            raise TypeError("capacity must be an integer")

        if capacity <= 0:
            raise ValueError("capacity must be greater than zero")

        self._capacity = capacity
        self._entries: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        """Return a cached value and promote it to most recently used."""
        try:
            value = self._entries.pop(key)
        except KeyError:
            return None

        # Dictionary order runs from least to most recently used. Re-inserting
        # this hit at the end protects it from the next capacity eviction.
        self._entries[key] = value
        return value

    def put(self, key: K, value: V) -> None:
        """Insert or update a value, evicting only the oldest entry if needed."""
        try:
            self._entries.pop(key)
        except KeyError:
            # Replacing an existing key does not consume another slot. Evict
            # only for a new key, using the first entry as the oldest access.
            if len(self._entries) >= self._capacity:
                self._entries.popitem(last=False)

        self._entries[key] = value

    def clear(self) -> None:
        """Drop all session cache entries."""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
