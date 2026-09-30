"""Fingerprint — call input_tokens to get the token list.

Spec (Conversation key): per request, call input_tokens -> token list T.
One call serves both identity (the conversation key) and the save-threshold
count. The server tokenizes with its own template + tokenizer, no model compute.
"""

from __future__ import annotations

import aiohttp


async def get_tokens(
    session: aiohttp.ClientSession, upstream: str, body: bytes
) -> list[int]:
    """Call POST /chat/completions/input_tokens and return the token list.

    session: an open aiohttp ClientSession.
    upstream: the upstream host:port.
    body: the chat body (JSON, same as /chat/completions).

    Returns the list of token ids (the conversation fingerprint).
    """
    url = f"http://{upstream}/chat/completions/input_tokens"
    async with session.post(
        url, data=body, headers={"Content-Type": "application/json"}
    ) as resp:
        data = await resp.json()
        # Accept {"tokens": [...]} or a bare list.
        if isinstance(data, dict) and "tokens" in data:
            return [int(t) for t in data["tokens"]]
        if isinstance(data, list):
            return [int(t) for t in data]
        raise ValueError(f"unexpected input_tokens response: {type(data)}")
