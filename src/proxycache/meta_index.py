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
import time
import logging
from typing import Dict, Optional, Tuple

from . import config
from . import hashing as hs
from .config import META_MAX_ENTRIES, META_MAX_SIZE_GB

log = logging.getLogger(__name__)

_GB = 1024 ** 3


class MetaIndex:
    def __init__(self, max_entries: int = META_MAX_ENTRIES):
        self.max_entries = max_entries
        self.max_size_gb = META_MAX_SIZE_GB
        # llama host's --slot-save-path; set at startup (empty = .bin ops off).
        self.save_path: str = ""
        self._metas: Dict[str, dict] = {}
        self._load()

    def _bin_reachable(self) -> bool:
        """True when the .bin directory exists in THIS proxy's filesystem."""
        return bool(self.save_path) and os.path.isdir(self.save_path)

    def _delete_bin(self, key: str) -> None:
        if not self._bin_reachable():
            return
        path = os.path.join(self.save_path, hs.bin_name(key))
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("bin_deleted key=%s path=%s", key[:16], path)
        except Exception as e:
            log.warning("bin_delete_fail key=%s: %s", key[:16], e)

    def clean_orphan_bins(self) -> list:
        """Delete .bin files in save_path whose key has no meta in the index.

        Returns the deleted keys. No-op when save_path is unreachable.
        """
        deleted = []
        if not self._bin_reachable():
            return deleted
        try:
            names = os.listdir(self.save_path)
        except OSError as e:
            log.warning("orphan_scan_fail: %s", e)
            return deleted
        for name in names:
            # Only proxy-owned files (pc_ prefix); llama's own
            # --cache-idle-slots files (same dir) are left alone.
            if not (name.startswith(hs.BIN_PREFIX) and name.endswith(".bin")):
                continue
            key = name[len(hs.BIN_PREFIX):-len(".bin")]
            if key not in self._metas:
                self._delete_bin(key)
                deleted.append(key)
        if deleted:
            log.info("orphan_bins_cleaned n=%d", len(deleted))
        return deleted

    def _load(self):
        for meta in hs.scan_all_meta():
            key = meta.get("key")
            if key:
                self._metas[key] = meta
        self._enforce_cap()
        log.info("meta_index_loaded n=%d", len(self._metas))

    def _total_size_bytes(self) -> int:
        return sum(m.get("size", 0) for m in self._metas.values())

    def write(
        self,
        key: str,
        prefix_text: str,
        blocks: list,
        wpb: int,
        model_id: str,
        unit: str = "words",
        size: int = 0,
    ) -> None:
        """Disk meta + index in one step. size = .bin file size in bytes."""
        import json
        now = time.time()
        meta = {
            "key": key,
            "model_id": model_id,
            "prefix_len": len(prefix_text),
            "wpb": wpb,
            "unit": unit,
            "blocks": blocks,
            "timestamp": now,
            "last_used": now,
            "size": size,
        }
        path = os.path.join(config.META_DIR, f"{key}.meta.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        self._metas[key] = meta
        self._enforce_cap()

    def update(self, key: str, meta: dict) -> None:
        self._metas[key] = meta
        self._enforce_cap()

    def touch_used(self, key: str) -> None:
        """Record a restore hit: bump last_used (memory + disk)."""
        meta = self._metas.get(key)
        if meta is None:
            return
        meta["last_used"] = time.time()
        path = os.path.join(config.META_DIR, f"{key}.meta.json")
        try:
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("meta_touch_fail key=%s: %s", key[:16], e)

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

    @staticmethod
    def _victim_key(meta: dict, policy: str) -> float:
        # Same ordering as gc._sort_key: "unused" = longest unused
        # (missing last_used falls back to timestamp), "created" = oldest.
        if policy == "unused":
            return meta.get("last_used", meta.get("timestamp", 0))
        return meta.get("timestamp", 0)

    def _enforce_cap(self) -> None:
        policy = config.META_MAX_ENTRIES_POLICY
        # Entry cap
        if self.max_entries > 0:
            while len(self._metas) > self.max_entries:
                self._evict_one(policy)
        # Size cap
        if self.max_size_gb > 0:
            max_bytes = self.max_size_gb * _GB
            while self._total_size_bytes() > max_bytes and self._metas:
                self._evict_one(policy)

    def _evict_one(self, policy: str) -> None:
        oldest = min(
            self._metas.values(),
            key=lambda m: self._victim_key(m, policy),
        )
        key = oldest.get("key")
        self._metas.pop(key, None)
        path = os.path.join(config.META_DIR, f"{key}.meta.json")
        try:
            if os.path.exists(path):
                os.remove(path)
            log.info("meta_gc key=%s", (key or "?")[:16])
        except Exception as e:
            log.warning("meta_gc_fail key=%s: %s", (key or "?")[:16], e)
        self._delete_bin(key or "")
