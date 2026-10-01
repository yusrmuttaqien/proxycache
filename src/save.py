"""Save — persist a conversation's slot to disk at the right moments, bounded.

Spec (Save): the proxy triggers POST /slots/{id}?action=save (the server does the
atomic .tmp+rename write), records the ledger, and evicts old files (LRU).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def should_save(
    response_complete: bool,
    l_tokens: int,
    min_save_tokens: int,
    conv_in_slot: bool,
    guard_on: bool,
) -> bool:
    """Save trigger: response fully received AND guard ON AND L >= min AND conv in slot.

    (design: event table — SAVE; flow step 6)
    """
    return (
        response_complete
        and guard_on
        and l_tokens >= min_save_tokens
        and conv_in_slot
    )


def delete_stale_tmp(save_root: str) -> list[str]:
    """Delete stale .tmp files at startup (server crashed mid-write).

    Returns the list of deleted paths. (design: Failure modes)
    """
    deleted: list[str] = []
    if not os.path.isdir(save_root):
        return deleted
    for dirpath, _dirnames, filenames in os.walk(save_root):
        for fn in filenames:
            if fn.endswith(".tmp"):
                path = os.path.join(dirpath, fn)
                os.remove(path)
                deleted.append(path)
    return deleted


@dataclass
class LedgerEntry:
    """A ledger entry: one saved file (exact sizes for LRU math)."""

    conv: str
    model: str
    path: str
    n_written: int  # bytes written
    n_saved: int  # KV entries saved
    saved_at: float
    last_used: float


class Ledger:
    """Tracks saved files: conv -> LedgerEntry. Used for LRU eviction."""

    def __init__(self, n_max_files: int = 4, max_gb: float = 2.0):
        self.n_max_files = n_max_files
        self.max_bytes = int(max_gb * 1024**3)  # GB -> bytes
        self.entries: dict[str, LedgerEntry] = {}

    def add(self, entry: LedgerEntry) -> None:
        self.entries[entry.conv] = entry

    def total_bytes(self) -> int:
        return sum(e.n_written for e in self.entries.values())

    def evict(self, protected: frozenset[str] = frozenset()) -> list[str]:
        """Evict oldest-used entries until within both caps. Never evicts protected
        (in-slot) convs. Returns the list of evicted conv ids."""
        evicted: list[str] = []
        sorted_entries = sorted(self.entries.items(), key=lambda x: x[1].last_used)
        for conv, _entry in sorted_entries:
            if len(self.entries) <= self.n_max_files and self.total_bytes() <= self.max_bytes:
                break
            if conv in protected:
                continue
            del self.entries[conv]
            evicted.append(conv)
        return evicted


class ThrashingGuard:
    """Detects A/B/A/B thrashing.

    Rolling window K; pause save+restore on >threshold switches; hysteresis to
    re-engage when switches drop to <=hysteresis. (design: flow step 8)
    """

    def __init__(self, window: int = 8, threshold: int = 1, hysteresis: int = 0):
        self.window = window
        self.threshold = threshold
        self.hysteresis = hysteresis
        self._history: list[str] = []
        self._paused = False

    def _count_switches(self) -> int:
        switches = 0
        for i in range(1, len(self._history)):
            if self._history[i] != self._history[i - 1]:
                switches += 1
        return switches

    def update(self, conv: str) -> bool:
        """Record a conv and return whether to pause save+restore."""
        self._history.append(conv)
        if len(self._history) > self.window:
            self._history.pop(0)
        switches = self._count_switches()
        if self._paused:
            if switches <= self.hysteresis:
                self._paused = False
        else:
            if switches > self.threshold:
                self._paused = True
        return self._paused
