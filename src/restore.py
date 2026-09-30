"""Restore — reload a conversation from disk exactly when it isn't in the live tree.

Spec (Restore): the proxy restores conv C into slot S when C ∉ ram_since_restore.
Restore is destructive (clears the live tree), so it must land on an empty slot
(drain-before-switch), and must save the displaced conv first.
"""

from __future__ import annotations

import os
from enum import Enum


class RestoreAction(str, Enum):
    FORWARD_WARM = "forward_warm"            # C in tree -> forward (LCP reuses)
    RESTORE = "restore"                      # C not in tree, file exists, no save needed
    SAVE_THEN_RESTORE = "save_then_restore"  # C not in tree, file exists, save first
    FORWARD_COLD = "forward_cold"            # C not in tree, no file (or can't restore yet)


def decide(
    conv: str,
    slot: int,
    desk,  # src.desk.Desk
    file_exists: bool,
    in_flight: int,
    slot_conv: str | None,
    save_threshold: int,
    slot_tokens: int,
) -> tuple[RestoreAction, str | None]:
    """Decide the restore action for a chat request.

    Returns (action, conv_to_save) — conv_to_save is the displaced conv to save
    first (if any). (design: event table — RESTORE; flow step 4-5)
    """
    # 1. C in the tree -> forward warm (server LCP-reuses, no restore).
    if desk.is_warm(slot, conv):
        return RestoreAction.FORWARD_WARM, None

    # 2. C not in the tree. Must drain first (in_flight == 0).
    if in_flight != 0:
        return RestoreAction.FORWARD_COLD, None

    # 3. Save the displaced conv first (if above threshold).
    conv_to_save = None
    if slot_conv is not None and slot_tokens >= save_threshold:
        conv_to_save = slot_conv

    # 4. Restore if file exists, else forward cold.
    if file_exists:
        action = (
            RestoreAction.SAVE_THEN_RESTORE if conv_to_save
            else RestoreAction.RESTORE
        )
        return action, conv_to_save
    return RestoreAction.FORWARD_COLD, None


def handle_restore_400(path: str) -> bool:
    """400 handling: bad/old file -> delete + continue cold.

    Returns True if the file was deleted, False if it wasn't there.
    (design: Failure modes) The file is corrupt or from an older context;
    delete it and continue cold (the server full-prefills).
    """
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def invalidate_all(desk) -> None:
    """Router restart / proxy restart -> all slots' sets = ∅.

    (design: Desk state — Invalidation events)
    """
    desk.mark_all_dirty()


def invalidate_model(desk, model: str) -> None:
    """Child unload/reload -> that model's sets = ∅.

    (design: Desk state — Invalidation events)
    """
    desk.mark_all_dirty()  # per-model: in a multi-model setup, filter by model
