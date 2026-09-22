# watcher.py

# -*- coding: utf-8 -*-

"""
B2/EV — model lifecycle watcher (llama.cpp as a sibling, no bundling).

Manages a SET of models (multi-model router safe). Two background loops
per backend:

1. SSE subscription to GET /models/sse (router):
   - model_status loading (a managed model that is NOT the slot's current
     one) -> save the current key NOW (KV dies on a --models-max 1 swap;
     we save BEFORE unload, not after);
   - unloaded (of the slot's current model) -> mark_cold (disk-only now);
   - loaded (of the slot's current model) -> reset the token baseline;
   - stream drop -> reconnect with backoff + reconcile (events may have
     been missed).

2. Periodic reconciler (every RECONCILE_INTERVAL seconds), tracking the
   slot's CURRENT model only:
   GET /props?model=  -> is_sleeping;
   GET /slots?model= -> n_prompt_tokens;
   a sharp n_prompt_tokens drop -> mark_cold (overflow/cache pressure).
"""

import asyncio
import json
import logging
from typing import List, Optional

from . import config
from .slot_manager import SlotManager
from .llama_client import LlamaClient

log = logging.getLogger(__name__)


class ModelWatcher:
    def __init__(self, be_id: int, client: LlamaClient, sm: SlotManager,
                 models: List[str]):
        self.be_id = be_id
        self.client = client
        self.sm = sm
        self.models = models
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
        backoff = config.SSE_RECONNECT_BACKOFF
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
        if model not in self.models:
            return

        g = self._find_g()
        slot_model = self.sm._slot_models.get(g) if g is not None else None

        if status == "loading":
            # A managed model other than the slot's is loading -> the slot's
            # KV is about to die. Save NOW.
            if g is not None:
                key = self.sm.slot_key(g)
                if key and model != slot_model:
                    if config.EVENT_SAVES:
                        asyncio.create_task(self._preemptive_save(g, key, slot_model))
        elif status == "unloaded":
            # Only an unload of the model the slot currently holds matters.
            if model == slot_model:
                self._mark_cold(model)
        elif status == "loaded":
            if model == slot_model:
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
            n_saved = await self.sm.save_after(g, key, slot_model)
            log.info("preemptive_save g=%s key=%s ok=%s bytes=%d", g, key[:16], n_saved > 0, n_saved)
        finally:
            self.sm.release(g)

    def _mark_cold(self, model: Optional[str]):
        g = self._find_g()
        if g is None:
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
            await asyncio.sleep(config.RECONCILE_INTERVAL)
            try:
                # Track only the model the slot currently holds.
                g = self._find_g()
                model = self.sm._slot_models.get(g) if g is not None else None
                if model is None or model not in self.models:
                    continue
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
                        key = self.sm.slot_key(g)
                        if key and self._last_prompt_tokens is not None:
                            # Sharp drop -> KV lost.
                            if n < self._last_prompt_tokens * config.KV_DROP_RATIO:
                                log.info(
                                    "reconcile_kv_drop n=%d was=%d",
                                    n, self._last_prompt_tokens,
                                )
                                self._mark_cold(model)
                        self._last_prompt_tokens = n
            except Exception as e:
                log.warning("reconcile_error: %s", e)
