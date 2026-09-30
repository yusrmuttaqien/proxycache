"""Shifted-suffix — detect rewrites, prune dead files, slide KV.

Spec (shifted-suffix): a *suffix* of the prompt is unchanged, but everything before
it shifted (compaction / head change / middle edit). If the new conv's tail matches
an old conv's tail (overlap >= tail_match_min), it's a shifted-suffix: the old file
is provably dead (delete it) and n_cache_reuse is injected on that one request.
"""

from __future__ import annotations


def detect_shifted_suffix(
    new_tokens: tuple[int, ...],
    old_tokens: tuple[int, ...],
    tail_match_min: int = 64,
) -> tuple[bool, int]:
    """Detect if new_tokens is a shifted-suffix of old_tokens.

    A shifted-suffix means the SUFFIX (last part) of new_tokens matches the
    SUFFIX of old_tokens (the head changed, the tail is unchanged).

    Returns (is_shifted, overlap) — overlap is the length of the matching
    suffix. (design: shifted-suffix — Detection threshold)
    """
    n_new = len(new_tokens)
    n_old = len(old_tokens)
    max_overlap = min(n_new, n_old)

    # Check if the suffix of new_tokens matches the suffix of old_tokens,
    # for the largest overlap >= tail_match_min.
    for overlap in range(max_overlap, tail_match_min - 1, -1):
        if new_tokens[-overlap:] == old_tokens[-overlap:]:
            return True, overlap
    return False, 0


def should_inject_n_cache_reuse(is_shifted: bool, targeted_mode: bool) -> bool:
    """True if n_cache_reuse should be injected.

    Targeted mode (preferred): only on the detected request (new conv whose tail
    matched an old tail) — exactly when a shifted suffix exists in the live tree.
    Off otherwise -> zero cost when unneeded. (design: shifted-suffix — n_cache_reuse)
    """
    return is_shifted and targeted_mode
