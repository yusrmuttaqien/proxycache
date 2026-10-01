"""HTTP client — upstream calls for the proxy's control plane.

Carries the api_key on all calls. Timeouts via control_timeout_ms.
(design: Operations — API key; Control-call failures)
"""

from __future__ import annotations

import aiohttp


class UpstreamClient:
    """HTTP client for the llama-server (upstream)."""

    def __init__(self, upstream: str, api_key: str = "", timeout_ms: int = 10000):
        self.upstream = upstream
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000.0)

    def _headers(self) -> dict:
        h: dict[str, str] = {}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def get_input_tokens(self, body: bytes) -> int | None:
        """POST /chat/completions/input_tokens — get the token count for a chat body.

        Returns the token count (L), or None on failure.

        NOTE: The endpoint returns the COUNT (input_tokens), not the LIST of tokens.
        The design assumed a token list; this is a mismatch. For the fingerprint,
        the count is used (sufficient for L >= min_save_tokens and basic identity).
        (design: input_tokens fingerprint)
        """
        url = f"http://{self.upstream}/chat/completions/input_tokens"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=body,
                    headers={
                        **self._headers(),
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("input_tokens")
                    return None
        except Exception:
            return None

    async def save_slot(self, slot: int, filename: str) -> dict | None:
        """POST /slots/{id}?action=save — save a slot to disk.

        Returns the response JSON (n_saved, n_written, etc.), or None on failure.
        (design: Primitives — save)
        """
        url = f"http://{self.upstream}/slots/{slot}?action=save"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"filename": filename},
                    headers=self._headers(),
                    timeout=self.timeout,
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception:
            return None

    async def restore_slot(self, slot: int, filename: str) -> dict | None:
        """POST /slots/{id}?action=restore — restore a slot from disk.

        Returns the response JSON, or None on failure (400 = bad file).
        (design: Primitives — restore)
        """
        url = f"http://{self.upstream}/slots/{slot}?action=restore"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"filename": filename},
                    headers=self._headers(),
                    timeout=self.timeout,
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception:
            return None

    async def erase_slot(self, slot: int) -> dict | None:
        """POST /slots/{id}?action=erase — erase a slot.

        Returns the response JSON, or None on failure.
        (design: Primitives — erase)
        """
        url = f"http://{self.upstream}/slots/{slot}?action=erase"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    headers=self._headers(),
                    timeout=self.timeout,
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception:
            return None

    async def get_slots(self, model: str) -> list | None:
        """GET /slots?model=<name> — get slot info for a model.

        Returns the list of slots, or None on failure.
        (design: Desk state — slot count)
        """
        url = f"http://{self.upstream}/slots?model={model}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self._headers(),
                    timeout=self.timeout,
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception:
            return None

    async def get_models(self) -> dict | None:
        """GET /models — get model info.

        Returns the response JSON, or None on failure.
        (design: config — Auto-detection)
        """
        url = f"http://{self.upstream}/models"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self._headers(),
                    timeout=self.timeout,
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception:
            return None
