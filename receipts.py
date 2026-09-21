# receipts.py

# -*- coding: utf-8 -*-

"""
A1 — «чеки» повторного использования KV.

llama.cpp отвечает timings.cache_n (токенов, взятых из KV) и
timings.prompt_n (пересчитанных). Отношение cache_n/(cache_n+prompt_n)
показывает, сколько запрос реально повторил кеш.

Для каждого meta-key ведём скользящий учёт:
- record(key, cache_n, prompt_n) — после каждого завершённого запроса;
- is_stale(key) — True, если последние N запросов к key почти ничего
  не повторили (кеш на диске/в слоте, вероятно, мёртвый);
- prune_stale() — удаляет meta-файлы stale-ключей (сам .bin на хосте
  llama.cpp остаётся; GC по размеру — P3).

Память-только: после рестарта прокси счётчики пустые, и это
корректно — первый запрос к key станет свежей проверкой.
"""

import os
import time
import logging
from typing import Dict

from config import META_DIR

log = logging.getLogger(__name__)

STALE_WINDOW = 2      # сколько последних чеков смотрим
STALE_RATIO = 0.2     # ниже этого по reuse — считаем мёртвым


class Receipts:
    def __init__(self):
        self._stats: Dict[str, Dict] = {}

    def record(self, key: str, cache_n: int, prompt_n: int) -> float:
        total = max(1, cache_n + prompt_n)
        ratio = cache_n / total
        s = self._stats.setdefault(key, {"ratios": [], "last": 0.0})
        s["ratios"].append(ratio)
        s["last"] = time.time()
        s["ratios"] = s["ratios"][-STALE_WINDOW:]
        log.debug("receipt key=%s cache_n=%d prompt_n=%d ratio=%.2f",
                  key[:16], cache_n, prompt_n, ratio)
        return ratio

    def is_stale(self, key: str) -> bool:
        s = self._stats.get(key)
        if not s or len(s["ratios"]) < STALE_WINDOW:
            return False
        return all(r < STALE_RATIO for r in s["ratios"][-STALE_WINDOW:])

    def forget(self, key: str) -> None:
        self._stats.pop(key, None)

    def prune_stale(self, keys: list) -> list:
        """Удаляет meta-файлы stale-ключей из переданного списка. Возвращает удалённые."""
        removed = []
        for key in keys:
            if self.is_stale(key):
                path = os.path.join(META_DIR, f"{key}.meta.json")
                try:
                    if os.path.exists(path):
                        os.remove(path)
                    removed.append(key)
                    self.forget(key)
                    log.info("prune_stale key=%s", key[:16])
                except Exception as e:
                    log.warning("prune_stale_fail key=%s: %s", key[:16], e)
        return removed
