"""Slot allocator — the proxy authoritatively assigns convs to slots.

Spec (Slot awareness): the proxy injects id_slot into every chat body (always on),
tracks the conv -> slot table, and allocates slots (new conv, all slots held ->
LRU conv's slot; save displaced first).
"""

from __future__ import annotations

import json


def inject_id_slot(body: bytes, slot: int) -> bytes:
    """Inject id_slot into the chat body (always on).

    body: the chat body (JSON).
    slot: the slot id to pin.
    Returns the modified body (JSON). The client never sees id_slot
    (it's a server-accepted field). (design: Slot awareness)
    """
    data = json.loads(body)
    data["id_slot"] = slot
    return json.dumps(data).encode()


class SlotAllocator:
    """Tracks the conv -> slot table and allocates slots.

    (design: Desk state — conv -> slot table; Slot awareness — Allocation)
    """

    def __init__(self, n_slots: int = 1):
        self.n_slots = n_slots
        self.conv_to_slot: dict[str, int] = {}  # conv -> slot
        self.slot_to_conv: dict[int, str] = {}  # slot -> conv

    def allocate(self, conv: str, lru_order: list[str] | None = None) -> tuple[int, str | None]:
        """Allocate a slot for a conv.

        Returns (slot, conv_to_evict) — conv_to_evict is the displaced conv
        (if any, when all slots are held). (design: Slot awareness — Allocation)

        lru_order: the convs in LRU order (oldest first). Used to pick which
        conv to evict when all slots are held.
        """
        # 1. Conv already has a slot -> reuse it.
        if conv in self.conv_to_slot:
            return self.conv_to_slot[conv], None

        # 2. Find a free slot.
        for slot in range(self.n_slots):
            if slot not in self.slot_to_conv:
                self.conv_to_slot[conv] = slot
                self.slot_to_conv[slot] = conv
                return slot, None

        # 3. All slots held -> evict the LRU conv's slot.
        if lru_order:
            evict_conv = lru_order[0]  # oldest
        else:
            evict_conv = next(iter(self.slot_to_conv.values()))
        slot = self.conv_to_slot[evict_conv]
        del self.conv_to_slot[evict_conv]
        del self.slot_to_conv[slot]
        self.conv_to_slot[conv] = slot
        self.slot_to_conv[slot] = conv
        return slot, evict_conv

    def get_slot(self, conv: str) -> int | None:
        """Get the slot for a conv (None if not allocated)."""
        return self.conv_to_slot.get(conv)

    def get_conv(self, slot: int) -> str | None:
        """Get the conv for a slot (None if not allocated)."""
        return self.slot_to_conv.get(slot)
