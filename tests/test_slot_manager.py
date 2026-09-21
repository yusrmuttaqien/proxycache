# tests/test_slot_manager.py

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import asyncio

from proxycache.slot_manager import SlotManager


class FakeClient:
    def __init__(self):
        self.saves = []
        self.restores = []

    async def save_slot(self, slot_id, basename, model=None):
        self.saves.append((slot_id, basename, model))
        return True

    async def restore_slot(self, slot_id, basename, model=None):
        self.restores.append((slot_id, basename, model))
        return True


def make_sm():
    sm = SlotManager.__new__(SlotManager)
    sm.backends = [{"id": 0, "client": FakeClient(), "n_slots": 1}]
    sm._all_slots = [(0, 0)]
    sm._last_used = {(0, 0): 0.0}
    import asyncio as _a
    sm._locks = {(0, 0): _a.Lock()}
    sm._slot_keys = {}
    sm._slot_models = {}
    sm._saved_len = {}
    sm._last_save = {}
    return sm


def test_acquire_release():
    async def run():
        sm = make_sm()
        g = await sm.acquire(acquire_timeout=1.0)
        assert g == (0, 0)
        sm.release(g)
        assert not sm._locks[g].locked()
    asyncio.run(run())


def test_table_only_on_confirmed_save():
    async def run():
        sm = make_sm()
        g = await sm.acquire(acquire_timeout=1.0)
        assert sm.slot_key(g) is None
        await sm.save_after(g, "keyA", "modelX", 100)
        assert sm.slot_key(g) == "keyA"
        sm.mark_cold(g)
        assert sm.slot_key(g) is None
        sm.release(g)
    asyncio.run(run())


def test_save_fuse():
    sm = make_sm()
    assert sm.save_allowed("k", 10)
    import time
    sm._last_save["k"] = time.time()
    assert not sm.save_allowed("k", 10)


def test_restore_updates_table():
    async def run():
        sm = make_sm()
        g = await sm.acquire(acquire_timeout=1.0)
        ok = await sm.restore(g, "keyR", "modelX")
        assert ok and sm.slot_key(g) == "keyR"
        sm.release(g)
    asyncio.run(run())
