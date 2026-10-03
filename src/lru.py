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
        """Evict entries to fit within budget (oldest-used first).

        Returns a list of (conv, path) pairs to evict.
        (design: LRU eviction — max_bytes, n_max_files)

        Policy: evict oldest-used entries until BOTH caps are satisfied.
        Whichever cap is binding (n_max_files or max_bytes) triggers eviction;
        multiple files are evicted in one pass if needed.
        """
        evictions: list[tuple[str, str]] = []

        while True:
            over_files = self.n_max_files > 0 and len(ledger) > self.n_max_files
            over_bytes = self.max_bytes > 0 and ledger.total_size() > self.max_bytes
            if not over_files and not over_bytes:
                break
            oldest = ledger.oldest()
            if oldest is None:
                break
            conv, entry = oldest
            evictions.append((conv, entry.path))
            ledger.remove(conv)

        return evictions
