"""Test runner — runs all phase tests and reports results.

Phase 9 — Testing: unit tests per phase (the "Done when" gates).
"""

import sys
import traceback

tests_passed = 0
tests_failed = 0


def test_phase0():
    """Phase 0 — Config."""
    from src.config import default_config
    c = default_config()
    assert c["min_save_tokens"] == 512
    assert c["tail_match_min"] == 64
    assert c["n_max_files"] == 4
    assert "listen" in c
    assert "upstream" in c
    assert "save_path" in c
    print("  OK Phase 0 — Config")


def test_phase1():
    """Phase 1 — Routes."""
    from src.routes import classify, Action
    assert classify("POST", "/chat/completions", None) == Action.INTERCEPT
    assert classify("GET", "/health", None) == Action.PASS
    assert classify("POST", "/slots/0", "action=save") == Action.PROXY_INTERNAL
    print("  OK Phase 1 — Routes")


def test_phase2():
    """Phase 2 — Conv key."""
    from src.convkey import ConvTracker
    tracker = ConvTracker(tail_len=3)
    conv1 = tracker.check((1, 2, 3, 4, 5, 6, 7), None)
    conv2 = tracker.check((1, 2, 3, 4, 5, 6, 7, 8), None)
    assert conv1 == conv2  # same conv
    conv3 = tracker.check((1, 2, 3, 4, 5, 8, 9), None)
    assert conv3 != conv1  # fork
    print("  OK Phase 2 — Conv key")


def test_phase3():
    """Phase 3 — Save."""
    from src.save import should_save, Ledger, LedgerEntry, ThrashingGuard
    assert should_save(True, 100, 10, True, True) is True
    ledger = Ledger(n_max_files=2, max_bytes=1000)
    now = 1000.0
    ledger.add(LedgerEntry("c1", "m", "p1", 400, 10, now, now))
    ledger.add(LedgerEntry("c2", "m", "p2", 400, 10, now, now + 1))
    ledger.add(LedgerEntry("c3", "m", "p3", 400, 10, now, now + 2))
    evicted = ledger.evict()
    assert evicted == ["c1"]
    guard = ThrashingGuard(window=8, threshold=1, hysteresis=0)
    assert guard.update("A") is False
    assert guard.update("B") is False
    assert guard.update("A") is True  # 2 switches > 1 -> pause
    print("  OK Phase 3 — Save")


def test_phase4():
    """Phase 4 — Restore."""
    from src.desk import Desk
    from src.restore import RestoreAction, decide
    desk = Desk()
    desk.add_forward(0, "convA")
    assert decide("convA", 0, desk, True, 0, None, 10, 0) == (RestoreAction.FORWARD_WARM, None)
    desk2 = Desk()
    assert decide("convB", 0, desk2, True, 0, None, 10, 0) == (RestoreAction.RESTORE, None)
    assert decide("convB", 0, desk2, False, 0, None, 10, 0) == (RestoreAction.FORWARD_COLD, None)
    print("  OK Phase 4 — Restore")


def test_phase5():
    """Phase 5 — Allocator."""
    import json
    from src.allocator import inject_id_slot, SlotAllocator
    out = inject_id_slot(b'{"messages": []}', 0)
    assert json.loads(out)["id_slot"] == 0
    alloc = SlotAllocator(n_slots=1)
    s, e = alloc.allocate("convA")
    assert s == 0 and e is None
    s, e = alloc.allocate("convB", lru_order=["convA"])
    assert e == "convA"
    print("  OK Phase 5 — Allocator")


def test_phase6():
    """Phase 6 — Shifted."""
    from src.shifted import detect_shifted_suffix, should_inject_n_cache_reuse
    is_shifted, overlap = detect_shifted_suffix(
        (100, 101, 4, 5, 6, 7, 8, 9, 10), (1, 2, 3, 4, 5, 6, 7, 8, 9, 10), tail_match_min=3
    )
    assert is_shifted is True and overlap == 7
    assert should_inject_n_cache_reuse(True, True) is True
    assert should_inject_n_cache_reuse(False, True) is False
    print("  OK Phase 6 — Shifted")


def test_phase7():
    """Phase 7 — Robustness."""
    from src.desk import Desk
    from src.robustness import (
        safe_parse_body, on_connection_drop, on_input_tokens_failure, HealthPoller,
    )
    assert safe_parse_body(b'{"messages": []}') == {"messages": []}
    assert safe_parse_body(b"not json") is None
    desk = Desk()
    desk.add_forward(0, "convA")
    on_connection_drop(desk)
    assert desk.is_warm(0, "convA") is False
    assert on_input_tokens_failure(1) == "retry"
    assert on_input_tokens_failure(2) == "degraded"
    poller = HealthPoller(1000)
    assert poller.should_poll(0.0) is True
    print("  OK Phase 7 — Robustness")


def test_phase8():
    """Phase 8 — Observability."""
    from src.observability import MissReason, classify_miss, echo_conversation_id
    assert classify_miss(False, True, 100, 10, False) == MissReason.NO_FILE
    assert classify_miss(True, False, 100, 10, False) == MissReason.RESTORE_400
    assert classify_miss(True, True, 5, 10, False) == MissReason.BELOW_MIN
    assert echo_conversation_id("conv123") == {"X-Conversation-Id": "conv123"}
    print("  OK Phase 8 — Observability")


def main() -> int:
    """Run all phase tests. Returns 0 if all pass, 1 if any fail."""
    tests = [
        test_phase0, test_phase1, test_phase2, test_phase3, test_phase4,
        test_phase5, test_phase6, test_phase7, test_phase8,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception:
            print(f"  FAIL {test.__doc__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
