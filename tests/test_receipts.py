# tests/test_receipts.py

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest

from proxycache import config
from proxycache.receipts import Receipts


@pytest.fixture
def tmp_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "META_DIR", str(tmp_path))
    return tmp_path


def test_stale_after_two_colds(tmp_meta):
    r = Receipts()
    r.record("k", 1, 999)
    assert not r.is_stale("k")
    r.record("k", 1, 999)
    assert r.is_stale("k")


def test_not_stale_with_reuse(tmp_meta):
    r = Receipts()
    r.record("k", 999, 1)
    r.record("k", 999, 1)
    assert not r.is_stale("k")


def test_prune_removes_meta(tmp_meta):
    import json
    path = os.path.join(config.META_DIR, "k.meta.json")
    with open(path, "w") as f:
        json.dump({"key": "k"}, f)
    r = Receipts()
    r.record("k", 1, 999)
    r.record("k", 1, 999)
    removed = r.prune_stale(["k"])
    assert removed == ["k"]
    assert not os.path.exists(path)
