"""File ledger — tracks conv -> file mapping with sizes.

(design: ledger — conv -> file, size tracking)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileEntry:
    """A ledger entry: conv -> file with size."""
    path: str
    size: int  # bytes


class FileLedger:
    """Tracks conv -> file mapping with sizes.

    (design: ledger — conv -> file, size tracking)
    """

    def __init__(self):
        self._entries: dict[str, FileEntry] = {}  # conv -> FileEntry

    def set(self, conv: str, path: str, size: int) -> None:
        """Add or update a ledger entry."""
        self._entries[conv] = FileEntry(path=path, size=size)

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

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, conv: str) -> bool:
        return conv in self._entries

    def items(self):
        """Iterate over (conv, FileEntry) pairs."""
        return self._entries.items()
