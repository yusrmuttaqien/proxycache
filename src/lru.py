"""LRU evictor — max_bytes + n_max_files policy.

(design: LRU eviction — max_bytes, n_max_files)
"""

from __future__ import annotations

from src.ledger import FileLedger


class LRUEvictor:
    """LRU eviction policy: max_bytes + n_max_files.

    (design: LRU eviction — max_bytes, n_max_files)
    """

    def __init__(self, max_bytes: int, n_max_files: int, buffer_pct: float = 0.05):
        self.max_bytes = max_bytes  # 0 = unlimited
        self.n_max_files = n_max_files  # 0 = unlimited
        # Buffer: evict slightly under the limit to avoid temporary overage.
        # Size: 5% buffer (evict at 95% of max_bytes).
        # Count: 1 file buffer (evict at n_max_files - 1).
        self.effective_max_bytes = int(max_bytes * (1 - buffer_pct)) if max_bytes > 0 else 0
        self.effective_n_max_files = (n_max_files - 1) if n_max_files > 0 else 0

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
            over_files = self.effective_n_max_files > 0 and len(ledger) > self.effective_n_max_files
            over_bytes = self.effective_max_bytes > 0 and ledger.total_size() > self.effective_max_bytes
            if not over_files and not over_bytes:
                break
            oldest = ledger.oldest()
            if oldest is None:
                break
            conv, entry = oldest
            evictions.append((conv, entry.path))
            ledger.remove(conv)

        return evictions
