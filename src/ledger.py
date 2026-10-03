"""File ledger — tracks conv -> file mapping with sizes + LRU order.

(design: ledger — conv -> file, size tracking, LRU eviction)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class FileEntry:
    """A ledger entry: conv -> file with size + last-used timestamp."""
    path: str
    size: int  # bytes
    last_used: float = field(default_factory=time.time)  # epoch seconds


class FileLedger:
    """Tracks conv -> file mapping with sizes + LRU order.

    (design: ledger — conv -> file, size tracking, LRU eviction)
    """

    def __init__(self):
        self._entries: dict[str, FileEntry] = {}  # conv -> FileEntry

    def set(self, conv: str, path: str, size: int) -> None:
        """Add or update a ledger entry (resets last_used)."""
        self._entries[conv] = FileEntry(path=path, size=size)

    def touch(self, conv: str) -> None:
        """Update last_used for a conv (called on save/restore access)."""
        entry = self._entries.get(conv)
        if entry is not None:
            entry.last_used = time.time()

    def get(self, conv: str) -> FileEntry | None:
        """Get a ledger entry (None if not found)."""
        return self._entries.get(conv)

    def remove(self, conv: str, delete_file: bool = False, full_path: str | None = None) -> None:
        """Remove a ledger entry. If delete_file, also delete the .bin file."""
        entry = self._entries.pop(conv, None)
        if delete_file and entry:
            import os
            path = full_path or entry.path
            try:
                os.unlink(path)
            except OSError:
                pass  # File already deleted or doesn't exist

    def total_size(self) -> int:
        """Total size of all entries (bytes)."""
        return sum(e.size for e in self._entries.values())

    def oldest(self) -> tuple[str, FileEntry] | None:
        """Return the (conv, entry) with the oldest last_used (LRU victim)."""
        if not self._entries:
            return None
        return min(self._entries.items(), key=lambda x: x[1].last_used)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, conv: str) -> bool:
        return conv in self._entries

    def items(self):
        """Iterate over (conv, FileEntry) pairs."""
        return self._entries.items()
