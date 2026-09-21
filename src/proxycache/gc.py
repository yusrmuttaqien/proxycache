# gc.py
# -*- coding: utf-8 -*-

"""
One-shot meta + .bin garbage collection (no server needed).

Usage:
    python proxycache.py --gc N --by created|unused

Selects N metas (oldest created, or longest unused) from META_DIR, deletes
each .meta.json, and — when the save path is known and reachable — the
matching .bin on the llama host's --slot-save-path.

The save path comes from config [meta] save_path; empty = auto-detect from
GET /models (the instance's command line carries --slot-save-path).
"""

import os
import asyncio
import logging

from . import config
from . import hashing as hs
from .llama_client import LlamaClient

log = logging.getLogger(__name__)


def _sort_key(meta: dict, by: str) -> float:
    if by == "unused":
        return meta.get("last_used", meta.get("timestamp", 0))
    return meta.get("timestamp", 0)


async def _detect_save_path() -> str:
    """Config override, else auto-detect from the first backend."""
    if config.SAVE_PATH:
        return config.SAVE_PATH
    be = config.BACKENDS[0] if config.BACKENDS else None
    if not be:
        return ""
    client = LlamaClient(be["url"])
    try:
        return await client.get_slot_save_path()
    finally:
        await client.close()


async def run_gc(n: int, by: str, dry_run: bool = False) -> int:
    metas = hs.scan_all_meta()
    if not metas:
        print("no metas found in", config.META_DIR)
        return 0

    metas = [m for m in metas if m.get("key")]
    metas.sort(key=lambda m: _sort_key(m, by))
    chosen = metas[:n]

    save_path = await _detect_save_path()
    print(f"meta dir : {config.META_DIR}")
    print(f"save path: {save_path or '(unknown — .bin deletion skipped)'}")
    print(f"policy   : {by}, evicting {len(chosen)} of {len(metas)}\n")

    rc = 0
    for meta in chosen:
        key = meta["key"]
        meta_path = os.path.join(config.META_DIR, f"{key}.meta.json")
        bin_path = os.path.join(save_path, f"{key}.bin") if save_path else ""

        print(f"{key[:16]}  created={meta.get('timestamp', 0):.0f} "
              f"last_used={meta.get('last_used', 0):.0f}")
        for path, kind in ((meta_path, "meta"), (bin_path, "bin")):
            if not path:
                continue
            if not os.path.exists(path):
                print(f"  {kind}: missing, skipped")
                continue
            if dry_run:
                print(f"  {kind}: would delete {path}")
            else:
                os.remove(path)
                print(f"  {kind}: deleted {path}")
    if dry_run:
        print("\n(dry run — nothing deleted)")
    return rc


def main(n: int, by: str, dry_run: bool = False) -> None:
    import sys
    sys.exit(asyncio.run(run_gc(n, by, dry_run)))
