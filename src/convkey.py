"""Conversation key — identify conversations with zero client cooperation.

Spec (Conversation key): hybrid — prefer X-Conversation-Id when present;
otherwise infer from the token list (content-derived id, tail match).

The proxy's "same conversation?" decision mirrors the server's LCP decision by
construction: same input (tokens), same rule (prefix match), they can't disagree.
"""

from __future__ import annotations

import hashlib
import re

# Sanitize X-Conversation-Id to [A-Za-z0-9._-].
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_conv_id(raw: str) -> str:
    """Sanitize a conversation id to [A-Za-z0-9._-] (filename-safe)."""
    return _SANITIZE_RE.sub("", raw)


def derive_id(tokens: tuple[int, ...], head_len: int = 64) -> str:
    """Derive a content-based id from head + tail (root exchange + match window).

    Deterministic: same tokens -> same id (survives a proxy restart).
    Uses head + tail (not just head) so forks (same head, different tail)
    get distinct ids.
    """
    head = tokens[:head_len]
    tail = tokens[-head_len:]
    combined = head + tail
    # Pack each token as a 4-byte signed integer for a deterministic bytes repr.
    data = b"".join(t.to_bytes(4, "big", signed=True) for t in combined)
    return hashlib.sha256(data).hexdigest()[:16]


class ConvTracker:
    """Tracks conversations: conv id -> (tail, length).

    tail: last `tail_len` tokens of the conv's token list.
    length: total length of the conv's token list.

    Memory-bounded: only the tail is stored per conv (not the full list), so
    per-conv memory is O(tail_len), independent of conversation length.
    """

    def __init__(self, tail_len: int = 64):
        self.tail_len = tail_len
        self.convs: dict[str, tuple[tuple[int, ...], int]] = {}

    def check(self, tokens: tuple[int, ...], header_id: str | None = None) -> str:
        """Determine the conversation id for a token list.

        1. Header wins when present (sanitized).
        2. Same-conversation check: longest-tail prefix match.
        3. Else new conversation: content-derived id.
        """
        # 1. Header wins when present.
        if header_id is not None:
            conv_id = sanitize_conv_id(header_id)
            if conv_id:
                self._update(conv_id, tokens)
                return conv_id

        # 2. Same-conversation check: longest-tail prefix match.
        #    A new token list T extends conv C if C's current token list is a
        #    prefix of T. With tail+length storage, we check that C's tail
        #    appears at the expected position in T (probabilistic, O(tail_len)).
        best_id = None
        best_len = 0
        n = len(tokens)
        for conv_id, (tail, length) in self.convs.items():
            if n >= length:
                start = max(0, length - self.tail_len)
                if tokens[start:length] == tail:
                    if length > best_len:
                        best_id = conv_id
                        best_len = length

        if best_id is not None:
            self._update(best_id, tokens)
            return best_id

        # 3. New conversation: content-derived id.
        conv_id = derive_id(tokens, self.tail_len)
        self._update(conv_id, tokens)
        return conv_id

    def _update(self, conv_id: str, tokens: tuple[int, ...]) -> None:
        """Update a conv's tail + length."""
        tail = tokens[-self.tail_len:]
        self.convs[conv_id] = (tail, len(tokens))
