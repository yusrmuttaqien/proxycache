# slot_manager.py

# -*- coding: utf-8 -*-

"""
Упрощённый SlotManager: только free/oldest по LRU, без hot/cold.

- get_slot(): сначала свободный (ещё не использовался), иначе самый старый по времени.
- Для big: если есть restore_key — делаем restore на выбранный слот.
- Сохранение всегда после завершения запроса.

Явная таблица слот→key (_slot_keys): обновляется ТОЛЬКО при реальном
успешном save/restore. Это единственное доверенное представление о том,
что лежит в слоте (см. upgrade.md B1).
"""

import time
import asyncio
import logging
from typing import List, Tuple, Dict, Optional

from config import BACKENDS

log = logging.getLogger(__name__)

GSlot = Tuple[int, int]  # (backend_id, local_slot_id)


class SlotManager:
    def __init__(self):
        self.backends = []
        total_slots = 0

        for be_id, conf in enumerate(BACKENDS):
            n_slots = int(conf["n_slots"])
            self.backends.append({"id": be_id, "client": None, "n_slots": n_slots})
            total_slots += n_slots

        self._all_slots: List[GSlot] = [
            (be_id, s)
            for be_id, be in enumerate(self.backends)
            for s in range(be["n_slots"])
        ]

        self._last_used: Dict[GSlot, float] = {g: 0.0 for g in self._all_slots}
        self._locks: Dict[GSlot, asyncio.Lock] = {
            g: asyncio.Lock() for g in self._all_slots
        }

        # B1: явная таблица «в слоте лежит key K» — только на основе
        # подтверждённых save/restore. model_id храним вместе с key.
        self._slot_keys: Dict[GSlot, str] = {}
        self._slot_models: Dict[GSlot, str] = {}

        log.info(
            "slot_manager n_backends=%d total_slots=%d",
            len(self.backends),
            total_slots,
        )

    def set_clients(self, clients: List):
        for i, client in enumerate(clients):
            self.backends[i]["client"] = client

    def _is_free(self, g: GSlot) -> bool:
        return self._last_used.get(g, 0.0) == 0.0

    def _get_free_or_oldest(self) -> Tuple[GSlot, asyncio.Lock]:
        free = [g for g in self._all_slots if self._is_free(g)]
        if free:
            g = free[0]
            return g, self._locks[g]

        g = sorted(self._all_slots, key=lambda x: self._last_used.get(x, 0.0))[0]
        return g, self._locks[g]

    async def acquire_for_request(
        self,
        restore_key: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[GSlot, asyncio.Lock, Optional[bool]]:
        g, lock = self._get_free_or_oldest()
        await lock.acquire()

        restored: Optional[bool] = None
        if restore_key:
            client = self.backends[g[0]]["client"]
            restored = await client.restore_slot(g[1], restore_key, model)
            log.info(
                "restore_before_chat g=%s key=%s ok=%s",
                g,
                (restore_key[:16] if restore_key else None),
                restored,
            )
            if restored:
                self._slot_keys[g] = restore_key
                if model:
                    self._slot_models[g] = model

        return g, lock, restored

    async def save_after(
        self,
        g: GSlot,
        key: str,
        model: Optional[str] = None,
    ) -> bool:
        client = self.backends[g[0]]["client"]
        ok = await client.save_slot(g[1], key, model)
        self._last_used[g] = time.time()
        if ok:
            # Таблицу трогаем только при подтверждённом save.
            self._slot_keys[g] = key
            if model:
                self._slot_models[g] = model
        return ok

    def mark_cold(self, g: GSlot) -> None:
        """KV в слоте потеряно (sleep/overflow/смена модели) — key живёт только на диске."""
        self._slot_keys.pop(g, None)
        self._slot_models.pop(g, None)

    def slot_key(self, g: GSlot) -> Optional[str]:
        return self._slot_keys.get(g)

    async def shutdown_save(self) -> None:
        """SIGTERM-сохранение: сохраним текущий key каждого занятого слота."""
        for g, key in list(self._slot_keys.items()):
            model = self._slot_models.get(g)
            try:
                lock = self._locks[g]
                if lock.locked():
                    log.info("shutdown_save_skip_locked g=%s key=%s", g, key[:16])
                    continue
                await lock.acquire()
                try:
                    ok = await self.save_after(g, key, model)
                    log.info("shutdown_save g=%s key=%s ok=%s", g, key[:16], ok)
                finally:
                    lock.release()
            except Exception as e:
                log.warning("shutdown_save_fail g=%s: %s", g, e)

    def release(self, g: GSlot):
        if self._locks[g].locked():
            self._locks[g].release()
