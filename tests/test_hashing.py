# tests/test_hashing.py

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashing as hs


def test_raw_prefix_joins_contents():
    msgs = [
        {"role": "system", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": None},
        {"role": "user", "content": "c"},
    ]
    assert hs.raw_prefix(msgs) == "a\n\nb\n\nc"


def test_words_and_blocks():
    text = " ".join(str(i) for i in range(250))
    blocks = hs.block_hashes_from_text(text, 100)
    assert len(blocks) == 3  # 100, 100, 50


def test_lcp():
    a = ["1", "2", "3"]
    b = ["1", "2", "4"]
    assert hs.lcp_blocks(a, b) == 2
    assert hs.lcp_blocks(a, ["x"]) == 0


def test_token_blocks():
    blocks = hs.token_blocks_from_ids(list(range(600)), 256)
    assert len(blocks) == 3
    # deterministic
    assert blocks == hs.token_blocks_from_ids(list(range(600)), 256)


def test_prefix_key_stable():
    k1 = hs.prefix_key_sha256("m\nhello")
    k2 = hs.prefix_key_sha256("m\nhello")
    assert k1 == k2
    assert k1 != hs.prefix_key_sha256("m\nworld")
