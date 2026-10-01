"""LRU evictor — max_bytes + n_max_files policy.

(design: LRU eviction — max_bytes, n_max_files)
"""

from __future__ import annotations

from src.ledger import FileLedger


class LRUEvictor:
    """LRU eviction policy: max_bytes + n_max_files.

    (design: LRU eviction — max_bytes, n_max_files)
    """

    def __init__(self, max_bytes: int, n_max_files: int):
        self.max_bytes = max_bytes  # 0 = unlimited
        self.n_max_files = n_max_files  # 0 = unlimited

    def evict(self, ledger: FileLedger) -> list[tuple[str, str]]:
        """Evict entries to fit within budget.

        Returns a list of (conv, path) pairs to evict.
        (design: LRU eviction — max_bytes, n_max_files)

        Policy:
        1. If n_max_files > 0 and len(ledger) > n_max_files:
           evict the oldest entries (by insertion order) until len <= n_max_files.
        2. If max_bytes > 0 and ledger.total_size() > max_bytes:
           evict the oldest entries until total_size <= max_bytes.

        NOTE: The ledger doesn't track insertion order (LRU order).
        For now, evict in arbitrary order (dict iteration order).
        TODO: Add LRU tracking to the ledger.
        """
        evictions: list[tuple[str, str]] = []

        # 1. n_max_files check.
        if self.n_max_files > 0:
            while len(ledger) > self.n_max_files:
                # Evict the first entry (arbitrary order for now).
                conv, entry = next(iter(ledger.items()))
                evictions.append((conv, entry.path))
                ledger.remove(conv)

        # 2. max_bytes check.
        if self.max_bytes > 0:
            while ledger.total_size() > self.max_bytes:
                conv, entry = next(iter(ledger.items()))
                evictions.append((conv, entry.path))
                ledger.remove(conv)

        return evictions
