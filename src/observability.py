"""Observability — every miss is diagnosable from logs.

Spec (Observability): the proxy logs n_prompt/n_kv_self/n_ctx per request,
ram_since_restore state, miss-reason classification, and echoes the
X-Conversation-Id header for client-side debugging.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MissReason(str, Enum):
    NO_FILE = "no_file"              # no file on disk
    RESTORE_400 = "400"              # restore failed (bad/old file)
    BELOW_MIN = "below_min"          # L < min_save_tokens
    THRASH_PAUSED = "thrash_paused"  # thrashing guard paused
    COLD = "cold"                    # C not in tree, forward cold


@dataclass
class RequestMetrics:
    """Per-request metrics (from the upstream response)."""
    n_prompt: int = 0
    n_kv_self: int = 0
    n_ctx: int = 0
    n_saved: int = 0
    n_written: int = 0


def classify_miss(
    file_exists: bool,
    restore_ok: bool,
    l_tokens: int,
    min_save_tokens: int,
    thrash_paused: bool,
) -> MissReason:
    """Classify the miss reason.

    (design: Observability — Miss debugger)
    """
    if thrash_paused:
        return MissReason.THRASH_PAUSED
    if l_tokens < min_save_tokens:
        return MissReason.BELOW_MIN
    if not file_exists:
        return MissReason.NO_FILE
    if not restore_ok:
        return MissReason.RESTORE_400
    return MissReason.COLD


def echo_conversation_id(conv_id: str) -> dict[str, str]:
    """Echo the X-Conversation-Id header (for client-side debugging).

    Returns the headers to add to the response.
    """
    return {"X-Conversation-Id": conv_id}


def log_ram_since_restore(desk, slot: int) -> str:
    """Log the ram_since_restore state for a slot.

    Returns a log string. "Warm" means the slot has any conv in
    ram_since_restore (not a specific conv).
    """
    warm = len(desk.ram_since_restore.get(slot, set())) > 0
    return f"slot={slot} ram_since_restore={'warm' if warm else 'cold'}"
