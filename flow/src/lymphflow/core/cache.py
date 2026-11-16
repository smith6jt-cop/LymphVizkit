"""Byte-bounded, thread-safe LRU for NumPy arrays (columns, compensated blocks, masks)."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable, Hashable

import numpy as np


class ArrayCache:
    def __init__(self, max_bytes: int = 4 * 1024**3):
        self.max_bytes = int(max_bytes)
        self._items: OrderedDict[Hashable, np.ndarray] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable) -> np.ndarray | None:
        with self._lock:
            arr = self._items.get(key)
            if arr is not None:
                self._items.move_to_end(key)
                self.hits += 1
            else:
                self.misses += 1
            return arr

    def put(self, key: Hashable, arr: np.ndarray) -> np.ndarray:
        arr.setflags(write=False)  # cached arrays are shared between requests
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old.nbytes
            if arr.nbytes <= self.max_bytes:
                self._items[key] = arr
                self._bytes += arr.nbytes
                while self._bytes > self.max_bytes and self._items:
                    _, evicted = self._items.popitem(last=False)
                    self._bytes -= evicted.nbytes
        return arr

    def get_or_compute(self, key: Hashable, compute: Callable[[], np.ndarray]) -> np.ndarray:
        arr = self.get(key)
        if arr is None:
            arr = self.put(key, compute())
        return arr

    def drop_prefix(self, prefix: tuple) -> None:
        """Remove every entry whose tuple key starts with `prefix` (e.g. a deleted sample)."""
        n = len(prefix)
        with self._lock:
            for key in [k for k in self._items if isinstance(k, tuple) and k[:n] == prefix]:
                self._bytes -= self._items.pop(key).nbytes

    @property
    def nbytes(self) -> int:
        return self._bytes
