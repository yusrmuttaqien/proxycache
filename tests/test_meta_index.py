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


def test_cap_evicts_bin_too(tmp_meta):
    import pathlib
    from proxycache.hashing import bin_name
    bin_dir = pathlib.Path(tmp_meta) / "bins"
    bin_dir.mkdir()
    idx = MetaIndex(max_entries=2)
    idx.save_path = str(bin_dir)
    blocks = ["h1"]
    for k in ("k1", "k2"):
        idx.write(k, k, blocks, 1, "m", "words")
        (bin_dir / bin_name(k)).write_bytes(b"x")
    idx.write("k3", "k3", blocks, 1, "m", "words")
    assert idx.get("k1") is None
    assert not (bin_dir / bin_name("k1")).exists()
    assert (bin_dir / bin_name("k2")).exists()


def test_clean_orphan_bins(tmp_meta):
    import pathlib
    from proxycache.hashing import bin_name
    bin_dir = pathlib.Path(tmp_meta) / "bins"
    bin_dir.mkdir()
    idx = MetaIndex(max_entries=8)
    idx.save_path = str(bin_dir)
    blocks = ["h1"]
    idx.write("known", "k", blocks, 1, "m", "words")
    (bin_dir / bin_name("known")).write_bytes(b"x")
    (bin_dir / bin_name("orphan")).write_bytes(b"x")
    # llama.cpp's own --cache-idle-slots file: same dir, no pc_ prefix.
    (bin_dir / "8759887b005ef509bdd1e32ae4ca4a4b4a65e402e5457daf2ab664270548f2e0.bin").write_bytes(b"x")
    (bin_dir / "notes.txt").write_text("t")
    deleted = idx.clean_orphan_bins()
    assert deleted == ["orphan"]
    assert (bin_dir / bin_name("known")).exists()
    assert (bin_dir / "8759887b005ef509bdd1e32ae4ca4a4b4a65e402e5457daf2ab664270548f2e0.bin").exists()
    assert (bin_dir / "notes.txt").exists()


def test_clean_orphan_bins_unreachable(tmp_meta):
    idx = MetaIndex(max_entries=8)
    idx.save_path = str(tmp_meta / "missing-dir")
    assert idx.clean_orphan_bins() == []
