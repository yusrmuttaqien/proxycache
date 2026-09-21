# hashing.py

# -*- coding: utf-8 -*-

"""
Raw prefix hashing.

raw_prefix: message contents only (no roles), joined with double newlines.
Blocks of 100 words, LCP over full SHA256 digests.
Key = sha256(model_id + "\\n" + raw_prefix) — the model is part of the key.

Meta files contain: key, model_id, prefix_len, wpb, blocks, timestamp.
"""

import os
import json
import hashlib
import re
import time
import glob
import logging
from typing import List, Dict, Optional, Tuple

from config import META_DIR, WORDS_PER_BLOCK

log = logging.getLogger(__name__)


def raw_prefix(messages: List[Dict]) -> str:
    parts = []
    for msg in messages or []:
        content = msg.get("content", "")
        if isinstance(content, str):
            content = content.strip()
        else:
            content = str(content).strip()
        if content:
            parts.append(content)
    text = "\n\n".join(parts).strip()
    log.debug("raw_prefix len_chars=%d", len(text))
    return text


def words_from_text(text: str) -> List[str]:
    return re.findall(r"\w+", text.lower())


def block_hashes_from_text(text: str, wpb: int = WORDS_PER_BLOCK) -> List[str]:
    words = words_from_text(text)
    hashes: List[str] = []
    for i in range(0, len(words), wpb):
        block = " ".join(words[i:i + wpb])
        h = hashlib.sha256(block.encode("utf-8")).hexdigest()
        hashes.append(h)
    log.debug("block_hashes n_blocks=%d wpb=%d", len(hashes), wpb)
    return hashes


def token_blocks_from_ids(tokens: List[int], tpb: int) -> List[str]:
    """A2: block hashes over TOKEN blocks (what llama.cpp actually caches)."""
    hashes: List[str] = []
    for i in range(0, len(tokens), tpb):
        block = " ".join(str(t) for t in tokens[i:i + tpb])
        hashes.append(hashlib.sha256(block.encode("utf-8")).hexdigest())
    log.debug("token_blocks n_blocks=%d tpb=%d", len(hashes), tpb)
    return hashes


def lcp_blocks(blocks1: List[str], blocks2: List[str]) -> int:
    n = min(len(blocks1), len(blocks2))
    i = 0
    while i < n and blocks1[i] == blocks2[i]:
        i += 1
    return i


def prefix_key_sha256(text: str) -> str:
    """
    SHA256 wrapper; callers pass model_id + "\\n" + raw_prefix.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan_all_meta() -> List[Dict]:
    files = sorted(
        glob.glob(os.path.join(META_DIR, "*.meta.json")),
        key=os.path.getmtime,
        reverse=True,
    )
    metas: List[Dict] = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fd:
                meta = json.load(fd)
                metas.append(meta)
        except Exception as e:
            log.warning("scan_meta_fail %s: %s", f, e)
    log.debug("scan_meta n_found=%d", len(metas))
    return metas


    path = os.path.join(META_DIR, f"{key}.meta.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

