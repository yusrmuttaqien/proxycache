# receipts.py

# -*- coding: utf-8 -*-

"""
A1 — KV reuse receipts.

llama.cpp reports timings.cache_n (tokens served from KV) and
timings.prompt_n (tokens recomputed). The ratio cache_n/(cache_n+prompt_n)
shows how much of the prompt the request actually reused.

Per meta-key we keep a rolling record:
- record(key, cache_n, prompt_n) — after every completed request;
- is_stale(key) — True if the last N requests to key reused almost
  nothing (the disk/slot cache is probably dead);
- prune_stale() — removes meta files of stale keys (the .bin on the
  llama.cpp host stays; size-based GC is P3).

Memory-only: after a proxy restart the counters are empty, which is
fine — the first request to a key becomes a fresh check.
"""

import os
import time
import logging
from typing import Dict

from . import config

log = logging.getLogger(__name__)

STALE_WINDOW = 2      # how many recent receipts to inspect
STALE_RATIO = 0.2     # below this reuse ratio the key is considered dead


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
        """Remove meta files of stale keys from the given list; returns removed keys."""
        removed = []
        for key in keys:
            if self.is_stale(key):
                path = os.path.join(config.META_DIR, f"{key}.meta.json")
                try:
                    if os.path.exists(path):
                        os.remove(path)
                    removed.append(key)
                    self.forget(key)
                    log.info("prune_stale key=%s", key[:16])
                except Exception as e:
                    log.warning("prune_stale_fail key=%s: %s", key[:16], e)
        return removed
