"""Route table — classify each request into a proxy action.

Spec (Endpoint routing): the route table decides what to do with each path.
The proxy is a transparent reverse proxy; this module decides, per request,
whether to intercept (chat), pass through (+ desk dirty), pure pass through,
or handle proxy-internally (save/restore/erase).
"""

from __future__ import annotations

from enum import Enum


class Action(str, Enum):
    INTERCEPT = "intercept"              # chat — full flow (parse body, route, save/restore)
    PASS_DESK_DIRTY = "pass_desk_dirty"  # pass-through + mark slot dirty
    PASS = "pass"                        # pure pass-through
    PROXY_INTERNAL = "proxy_internal"    # save/restore/erase (proxy's own control calls)


# Chat endpoints (intercepted — full flow). All POST.
CHAT_PATHS = frozenset({
    "/chat/completions",
    "/v1/chat/completions",
})

# Pass-through + desk dirty: (method, path) pairs that use a slot.
DESK_DIRTY = frozenset({
    ("POST", "/completion"),
    ("POST", "/completions"),
    ("POST", "/v1/completions"),
    ("POST", "/responses"),
    ("POST", "/v1/messages"),
    ("POST", "/infill"),
    ("POST", "/v1/audio/transcriptions"),
    ("POST", "/v1/chat/completions/control"),
    ("POST", "/embedding"),
    ("POST", "/embeddings"),
    ("POST", "/v1/embeddings"),
    ("POST", "/rerank"),
    ("POST", "/reranking"),
    ("POST", "/lora-adapters"),
    ("POST", "/models"),
    ("POST", "/models/load"),
    ("POST", "/models/unload"),
    ("POST", "/models/reload"),
    ("DELETE", "/models"),
    ("DELETE", "/v1/stream"),
})

_SAVE_RESTORE_ERASE = frozenset({"save", "restore", "erase"})


def is_slots_id(path: str) -> bool:
    """True if path is /slots/{id} (a specific slot, not /slots)."""
    parts = path.strip("/").split("/")
    return len(parts) == 2 and parts[0] == "slots" and parts[1] != ""


def _action_in(query: str, actions: frozenset) -> bool:
    """True if the query string contains action=<one of actions>."""
    if not query:
        return False
    for pair in query.split("&"):
        if pair.startswith("action="):
            return pair.split("=", 1)[1] in actions
    return False


def classify(method: str, path: str, query: str = "") -> Action:
    """Classify a request into a proxy action.

    method: HTTP method (e.g., "POST").
    path: the raw path (no query string), e.g., "/v1/chat/completions".
    query: the query string (e.g., "action=save").
    """
    method = method.upper()

    # Proxy-internal: POST /slots/{id}?action=save|restore|erase
    if method == "POST" and is_slots_id(path) and _action_in(query, _SAVE_RESTORE_ERASE):
        return Action.PROXY_INTERNAL

    # Intercepted: chat
    if method == "POST" and path in CHAT_PATHS:
        return Action.INTERCEPT

    # Pass-through + desk dirty
    if (method, path) in DESK_DIRTY:
        return Action.PASS_DESK_DIRTY
    # POST /slots/{id} (no action) → desk dirty
    if method == "POST" and is_slots_id(path):
        return Action.PASS_DESK_DIRTY

    # Pure pass-through
    return Action.PASS
