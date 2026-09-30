"""Robustness — the proxy's own failures degrade to cold, never down.

Spec (Failure modes): the proxy's control calls (save/restore) can time out or
fail. On failure, the proxy degrades to cold (forward the request), never
dropping the request. Body parse fail -> pass through unmodified.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


def safe_parse_body(body: bytes) -> dict | None:
    """Parse the chat body. Returns None on failure (pass through unmodified).

    (design: Failure modes — body parse fail)
    """
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def handle_control_call_failure(call_type: str) -> str:
    """Handle a control-call failure (save/restore timeout or 500).

    Returns the degradation action:
    - save failure -> "skip_save" (log + skip this turn; retry next turn)
    - restore failure -> "delete_cold" (delete file + continue cold)

    (design: Failure modes)
    """
    if call_type == "save":
        return "skip_save"
    elif call_type == "restore":
        return "delete_cold"
    else:
        return "skip"


@dataclass
class TimeoutConfig:
    """Control-call timeout configuration."""
    control_timeout_ms: int = 2000


def should_timeout(elapsed_ms: int, config: TimeoutConfig) -> bool:
    """True if the control call exceeded the timeout."""
    return elapsed_ms > config.control_timeout_ms


def on_mid_stream_disconnect(conv: str, partial_tokens: int, min_save_tokens: int) -> bool:
    """True if the partial state should be saved on mid-stream disconnect.

    (design: Operations #2 — turn boundary includes client disconnect)
    """
    return partial_tokens >= min_save_tokens


def on_connection_drop(desk) -> None:
    """Connection drop -> fail fast 502 + mark all desks empty.

    (design: Operations — connection drop)
    """
    desk.mark_all_dirty()


def on_input_tokens_failure(attempt: int) -> str:
    """Handle an input_tokens failure.

    Returns "retry" if attempt < 2, else "degraded" (pass-through + desk dirty).
    (design: Operations — input_tokens failure)
    """
    return "retry" if attempt < 2 else "degraded"


class HealthPoller:
    """Health polling for router-restart detection.

    (design: Desk state — Lifecycle)
    """

    def __init__(self, health_poll_ms: int = 1000):
        self.health_poll_ms = health_poll_ms
        self._last_healthy: float | None = None

    def record_healthy(self, now: float) -> None:
        self._last_healthy = now

    def should_poll(self, now: float) -> bool:
        """True if it's time to poll the health endpoint."""
        if self._last_healthy is None:
            return True
        return (now - self._last_healthy) * 1000 >= self.health_poll_ms
