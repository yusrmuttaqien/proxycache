"""Desk state — how the proxy knows what's in the slot.

Spec (Desk state): the proxy never queries llama.cpp for KV state — it
orchestrates every action that touches the slot, so it knows each slot's
contents from its own action log.

Phase 1 scope: slot count (from GET /slots) + desk-dirty tracking
(ram_since_restore per slot).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Desk:
    """Per-model desk state.

    n_slots: number of slots (from GET /slots response).
    ram_since_restore: per-slot set of convs forwarded since the last restore
        (= what's in that slot's live tree). ∅ = desk dirty (next chat is a
        restore candidate).
    """

    n_slots: int = 0
    ram_since_restore: dict[int, set[str]] = field(default_factory=dict)

    def set_slot_count(self, n: int) -> None:
        """Update the slot count (from GET /slots response)."""
        self.n_slots = n

    def mark_dirty(self, slot: int) -> None:
        """Invalidate one slot's ram_since_restore (desk dirty)."""
        self.ram_since_restore[slot] = set()

    def mark_all_dirty(self) -> None:
        """Invalidate all slots' ram_since_restore (server/proxy restart,
        child unload/reload)."""
        for slot in list(self.ram_since_restore):
            self.ram_since_restore[slot] = set()

    def is_warm(self, slot: int, conv: str) -> bool:
        """True if conv's KV is already in the slot's live tree (no API call)."""
        return conv in self.ram_since_restore.get(slot, set())

    def add_forward(self, slot: int, conv: str) -> None:
        """Record a forward (conv C into slot S): ram_since_restore ∪= {C}."""
        self.ram_since_restore.setdefault(slot, set()).add(conv)

    def set_restore(self, slot: int, conv: str) -> None:
        """Record a restore (conv C into slot S): ram_since_restore = {C}."""
        self.ram_since_restore[slot] = {conv}
