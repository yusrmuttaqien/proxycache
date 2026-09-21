# watcher.py

# -*- coding: utf-8 -*-

"""
B2/EV — model lifecycle watcher (llama.cpp as a sibling, no bundling).

Two background loops per backend:

1. SSE subscription to GET /models/sse (router):
   - model_status loading (a foreign model) -> save the current key NOW
     (KV dies on a --models-max 1 swap; we save BEFORE unload, not after);
   - unloaded -> mark_cold (the key is disk-only now);
   - loaded   -> table starts empty; the first request restores from disk;
   - stream drop -> reconnect with backoff + reconcile (events may have
     been missed).

2. Periodic reconciler (every RECONCILE_INTERVAL seconds):
   GET /props?model=  -> is_sleeping;
   GET /slots?model= -> n_prompt_tokens;
   a sharp n_prompt_tokens drop -> mark_cold (overflow/cache pressure).
"""

import asyncio
import json
import logging
from typing import Optional

from slot_manager import SlotManager
from llama_client import LlamaClient

log = logging.getLogger(__name__)

RECONCILE_INTERVAL = 20.0
SSE_RECONNECT_BACKOFF = 5.0


class ModelWatcher:
    def __init__(self, be_id: int, client: LlamaClient, sm: SlotManager,
                 model: Optional[str] = None):
        self.be_id = be_id
        self.client = client
        self.sm = sm
        self.model = model
        self._stop = False
        self._last_prompt_tokens: Optional[int] = None

    def stop(self):
        self._stop = True

    async def start(self):
        self._tasks = [
            asyncio.create_task(self._sse_loop()),
            asyncio.create_task(self._reconcile_loop()),
        ]

    async def _sse_loop(self):
        backoff = SSE_RECONNECT_BACKOFF
        while not self._stop:
            resp = await self.client.models_sse()
            if resp is None:
                await asyncio.sleep(backoff)
                continue
            try:
                event_name = "message"
                async for raw in resp.aiter_lines():
                    line = raw.strip()
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            try:
                                data = json.loads(payload)
                            except Exception:
                                continue
                            self._on_event(event_name, data)
                        else:
                            # data: [DONE] — stream closed, reconnect
                            break
            finally:
                await resp.aclose()
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _on_event(self, event: str, data: dict):
        if event != "model_status":
            log.debug("watcher_event_skip event=%s", event)
            return
        model = data.get("model")
        status = data.get("status")
        log.info("watcher_model_status model=%s status=%s", model, status)

        if status == "loading":
            # A foreign model is loading -> current KV is about to die. Save NOW.
            g = self._find_g()
            if g is not None:
                key = self.sm.slot_key(g)
                slot_model = self.sm._slot_models.get(g)
                if key and model != slot_model:
                    asyncio.create_task(self._preemptive_save(g, key, slot_model))
        elif status == "unloaded":
            self._mark_cold(model)
        elif status == "loaded":
            if model == self.model:
                self._last_prompt_tokens = None
        elif status == "sleeping":
            # sleep: KV is in RAM, slot state intact — not cold.
            pass

    async def _preemptive_save(self, g, key, slot_model):
        lock = self.sm._locks[g]
        if lock.locked():
            log.warning("preemptive_save_skip_locked g=%s key=%s", g, key[:16])
            return
        await lock.acquire()
        try:
            ok = await self.sm.save_after(g, key, slot_model)
            log.info("preemptive_save g=%s key=%s ok=%s", g, key[:16], ok)
        finally:
            self.sm.release(g)

    def _mark_cold(self, model: Optional[str]):
        g = self._find_g()
        if g is None:
            return
        # Only react to unload of the model we are watching.
        if model and self.model and model != self.model:
            return
        key = self.sm.slot_key(g)
        if key:
            self.sm.mark_cold(g)
            log.info("watcher_mark_cold g=%s key=%s", g, key[:16])
        self._last_prompt_tokens = None

    def _find_g(self):
        for g in self.sm._all_slots:
            if g[0] == self.be_id:
                # One slot per model in the current deployment; take the first.
                return g
        return None

    async def _reconcile_loop(self):
        while not self._stop:
            await asyncio.sleep(RECONCILE_INTERVAL)
            try:
                model = self.model
                props = await self.client.get_props(model)
                slots = await self.client.get_slots(model)
                if props is None and slots is None:
                    continue
                # sleep is not cold (KV in RAM, slot alive).
                if props and props.get("is_sleeping"):
                    self._last_prompt_tokens = None
                    continue
                if slots:
                    n = slots[0].get("n_prompt_tokens")
                    if isinstance(n, int):
                        key = self.sm.slot_key(self._find_g()) if self._find_g() else None
                        if key and self._last_prompt_tokens is not None:
                            # Sharp drop -> KV lost.
                            if n < self._last_prompt_tokens * 0.1:
                                log.info(
                                    "reconcile_kv_drop n=%d was=%d",
                                    n, self._last_prompt_tokens,
                                )
                                self._mark_cold(model)
                        self._last_prompt_tokens = n
            except Exception as e:
                log.warning("reconcile_error: %s", e)


class _dummy:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False
