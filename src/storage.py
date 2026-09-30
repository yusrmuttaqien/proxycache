"""Storage layout — {model}/{conv}.bin path.

Spec (Storage layout): one file per conversation, under {root}/{model}/.
"""

from __future__ import annotations

import os


def conv_path(save_root: str, model: str, conv: str) -> str:
    """Compute the {root}/{model}/{conv}.bin path for a conversation file."""
    return os.path.join(save_root, model, f"{conv}.bin")
