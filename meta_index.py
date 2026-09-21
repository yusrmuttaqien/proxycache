# meta_index.py

# -*- coding: utf-8 -*-

"""
P3: in-memory meta index.

The index is the single source for candidate search; disk meta files are
the persistence layer. Loaded at startup, updated on every write_meta,
enforces a size/entry cap (LRU by timestamp, receipt-informed).

The .bin files live on the llama.cpp host — the proxy can only remove the
meta side; orphaned .bin files are GC'd by size separately (documented).
"""

import os
import json
import time
import logging
from typing import Dict, Optional, Tuple

import hashing as hs
from config import META_DIR

log = logging.getLogger(__name__)

META_MAX_ENTRIES = int(os.getenv("META_MAX_ENTRIES", "32"))


class MetaIndex:
    def __init__(self, max_entries: int = META_MAX_ENTRIES):
        self.max_entries = max_entries
        self._metas: Dict[str, dict] = {}
        self._load()

    def _load(self):
        for meta in hs.scan_all_meta():
            key = meta.get("key")
            if key:
                self._metas[key] = meta
        log.info("meta_index_loaded n=%d", len(self._metas))

    def write(
        self,
        key: str,
        prefix_text: str,
        blocks: list,
        wpb: int,
        model_id: str,
        unit: str = "words",
    ) -> None:
        """Disk meta + index in one step."""
        import json
        meta = {
            "key": key,
            "model_id": model_id,
            "prefix_len": len(prefix_text),
            "wpb": wpb,
            "unit": unit,
            "blocks": blocks,
            "timestamp": time.time(),
        }
        path = os.path.join(META_DIR, f"{key}.meta.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        self._metas[key] = meta
        self._enforce_cap()

    def update(self, key: str, meta: dict) -> None:
        self._metas[key] = meta
        self._enforce_cap()

    def remove(self, key: str) -> None:
        self._metas.pop(key, None)

    def get(self, key: str) -> Optional[dict]:
        return self._metas.get(key)

    def keys(self) -> list:
        return list(self._metas.keys())

    def find_best(
        self,
        req_blocks: list,
        wpb: int,
        th: float,
        model_id: str,
        unit: str = "words",
    ) -> Optional[Tuple[str, float]]:
        best_key: Optional[str] = None
        best_ratio = 0.0
        for meta in self._metas.values():
            if meta.get("model_id") != model_id:
                continue
            if int(meta.get("wpb") or 0) != wpb:
                continue
            if meta.get("unit", "words") != unit:
                continue
            cand_blocks = meta.get("blocks") or []
            lcp = hs.lcp_blocks(req_blocks, cand_blocks)
            denom = max(1, min(len(req_blocks), len(cand_blocks)))
            ratio = lcp / denom
            if ratio >= th and ratio > best_ratio:
                best_ratio = ratio
                best_key = meta.get("key")
        return (best_key, best_ratio) if best_key else None

    def _enforce_cap(self) -> None:
        while len(self._metas) > self.max_entries:
            oldest = min(self._metas.values(), key=lambda m: m.get("timestamp", 0))
            key = oldest.get("key")
            self._metas.pop(key, None)
            path = os.path.join(META_DIR, f"{key}.meta.json")
            try:
                if os.path.exists(path):
                    os.remove(path)
                log.info("meta_gc key=%s", (key or "?")[:16])
            except Exception as e:
                log.warning("meta_gc_fail key=%s: %s", (key or "?")[:16], e)
