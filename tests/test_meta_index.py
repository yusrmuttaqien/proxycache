# tests/test_meta_index.py

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest

from proxycache import config
from proxycache import hashing as hs
from proxycache.meta_index import MetaIndex


@pytest.fixture
def tmp_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DIR", str(tmp_path))
    return tmp_path


def test_write_and_find(tmp_meta):
    idx = MetaIndex(max_entries=8)
    blocks = hs.block_hashes_from_text(" ".join(str(i) for i in range(400)), 100)
    idx.write("keyA", "prefix text", blocks, 100, "modelX", "words")

    found = idx.find_best(blocks, 100, 0.6, "modelX", "words")
    assert found is not None and found[0] == "keyA"
    assert found[1] == 1.0


def test_find_filters_model_and_unit(tmp_meta):
    idx = MetaIndex(max_entries=8)
    blocks = hs.block_hashes_from_text("abc def", 100)
    idx.write("keyA", "x", blocks, 100, "modelX", "words")
    assert idx.find_best(blocks, 100, 0.6, "otherModel", "words") is None
    assert idx.find_best(blocks, 100, 0.6, "modelX", "tokens") is None


def test_cap_evicts_oldest(tmp_meta):
    idx = MetaIndex(max_entries=2)
    blocks = ["h1"]
    idx.write("k1", "a", blocks, 1, "m", "words")
    idx.write("k2", "b", blocks, 1, "m", "words")
    idx.write("k3", "c", blocks, 1, "m", "words")
    assert idx.get("k1") is None
    assert idx.get("k3") is not None
    assert not os.path.exists(os.path.join(config.META_DIR, "k1.meta.json"))


def test_cap_evicts_unused_policy(tmp_meta, monkeypatch):
    monkeypatch.setattr(config, "META_MAX_ENTRIES_POLICY", "unused")
    idx = MetaIndex(max_entries=2)
    blocks = ["h1"]
    idx.write("k1", "a", blocks, 1, "m", "words")
    idx.write("k2", "b", blocks, 1, "m", "words")
    # k1 was created first but k2 is now the longest-unused.
    idx._metas["k2"]["last_used"] = 1.0
    idx.write("k3", "c", blocks, 1, "m", "words")
    assert idx.get("k2") is None
    assert idx.get("k1") is not None
    assert idx.get("k3") is not None


def test_persistence_reload(tmp_meta):
    idx = MetaIndex(max_entries=8)
    blocks = ["h1"]
    idx.write("keyP", "p", blocks, 1, "m", "words")
    idx2 = MetaIndex(max_entries=8)
    assert idx2.get("keyP") is not None
