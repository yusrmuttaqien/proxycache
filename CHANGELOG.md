# Changelog — proxycache

Build log + phase tracker. **Read this before working; append after every meaningful
step.** Spec: `llamacpp-autosave-proxy-design.md` (source of truth). Workflow:
`AGENTS.md`.

**Log entry format** (append, never overwrite user entries):

```
## [YYYY-MM-DD] — Session N
**Task**: <what was requested>
**Changes**:
- <file> — <what changed>
**Lessons**: <reusable insight, if any>
```

## Phases

| # | Phase | Status | Notes |
|---|---|---|---|
| 0 | Scaffolding | 🔨 | entry point, config (load/generate), transparent forward |
| 1 | Endpoint routing | ✅ | route table, desk-dirty, slot count, `{model}/{conv}.bin` |
| 2 | Conversation keying | ✅ | content-derived id, tail match, forks, sanitize |
| 3 | Save | ✅ | trigger, tmp+rename, ledger, LRU, thrashing guard |
| 4 | Restore | ✅ | trigger, drain, idempotent, invalidation, `erase` |
| 5 | Slot allocator | ✅ | `id_slot`, conv→slot, allocation |
| 6 | Shifted-suffix | ✅ | detect+delete, `n_cache_reuse` |
| 7 | Robustness | ✅ | 400/cold, degraded, timeouts, conn drop, health poll |
| 8 | Observability / Miss debugger | ✅ | taxonomy, artifacts, log levels |
| 9 | Testing | ⚠️ | unit ✅, integration ⬜, property ⬜ (need real server) |

Status legend: ⬜ not started · 🔨 in progress · ✅ done (+tested)

---

## Work queue (detailed, per phase)

Each task cites the **design section** it implements
(`llamacpp-autosave-proxy-design.md`). Check a box only when the task lands **and** is
tested. Phases are ordered so each is independently runnable.

### Phase 0 — Scaffolding

Goal: a config-driven transparent reverse proxy that forwards a chat request
byte-identically. **Structure mirrors `skill-sync-script`.**

- ✅ Entry point — `proxycache.py` (thin wrapper) → `src/__main__.py`
      (`argparse`, `main() -> int`) → `src/config.py`. Run:
      `venv/bin/python proxycache.py [flags]`. *(mirrors skill-sync-script)*
- ✅ Flags — `--version`, `--config <path>`, `--generate-config`. **No TUI**: default
      (no flags) = **serve the proxy**. *(design: Configuration)*
- ✅ Config system — single `config.json`; `--generate-config` emits every key with
      default + accepted values + description (self-documenting, skillsync-style);
      auto-generate with defaults if missing; precedence file > default.
      *(design: Configuration — Knobs table)*
- ✅ `requirements.txt` — pinned deps (aiohttp 3.14.3); use the provided `venv/` (Python 3.12).
- ✅ Transparent forward — all requests proxied, responses streamed back
      byte-identical. *(design: Transparency)*
- ⬜ **Deployment prerequisites** — server port reachable **only via the proxy**
      (else the action log goes stale silently); **shared filesystem** — the server
      *writes* `.bin` files (`--slot-save-path`), the proxy *deletes* them, both see
      the same path. *(design: Transparency — Deployment rule; Operations #5)*

**Done when:** `venv/bin/python proxycache.py` serves; a `/chat/completions` request
flows proxy→server→client byte-identical; `--generate-config` prints the self-doc
config; `config.json` auto-creates with defaults.

### Phase 1 — Endpoint routing

Goal: intercept chat; pass-through everything else; track desk dirtiness.

- ✅ Route table — intercept chat endpoints (full flow); pass-through the rest.
      *(design: Transparency — endpoint table)*
- ✅ Desk-dirty marking — non-chat slot mutators (completion/embedding/rerank/
      slots-no-action/models-lifecycle/stream) invalidate `ram_since_restore`.
      *(design: Transparency — Desk dirty)*
- ✅ Slot count read at startup per model (`GET /slots`). *(design: Slot awareness)*
- ✅ **Filename namespacing** — `{model}/{conv}.bin` (per-model subdirs). *(design:
      Deployment; Resolved decisions — Multi-model)*

**Done when:** chat is intercepted; an `/embeddings` call marks the desk dirty;
slot count is known; files land under `{model}/`.

### Phase 2 — Conversation keying

Goal: identify conversations with zero client cooperation.

- ✅ `input_tokens` fingerprint per chat request. *(design: Conversation key)*
- ✅ Memory-bounded parse — keep head (root exchange) + tail (match window),
      drop the middle. *(design: Conversation key — Memory-bounded parse)*
- ✅ Same-conversation check — longest-tail prefix match. *(design: Conversation key)*
- ✅ New conversation — content-derived id (hash of root exchange); deterministic.
      *(design: Conversation key)*
- ✅ Fork handling — branch-split when two convs extend the same tail then diverge.
      *(design: Conversation key — Forks)*
- ✅ `X-Conversation-Id` hybrid — header wins when present; sanitize to
      `[A-Za-z0-9._-]`. *(design: Conversation key — Known limitations)*

**Done when:** two turns of a conv → same id; a fork → two ids; a declared header id
is respected and sanitized.

### Phase 3 — Save

Goal: persist a conversation's slot to disk at the right moments, bounded.

- ✅ Save trigger — response fully received AND guard ON AND `L ≥ min_save_tokens`
      AND conv in slot table. *(design: event table — SAVE; flow step 6)*
- ✅ Turn boundary includes **client disconnect mid-stream** (save the partial state).
      *(design: Operations #2)*
- ✅ Save failure (disk full / 500) → log + skip this turn; retry next turn
      (idempotent, latest wins). *(design: Operations #3)*
- ✅ `.tmp` + rename — atomic write; delete stale `.tmp` at startup.
      *(design: Failure modes)*
- ✅ Ledger — `n_written`/`n_saved` accounting for exact sizes. *(design: flow step 6)*
- ✅ LRU eviction — `n_max_files` / `max_bytes`; evict oldest-used, never an
      in-slot conv. *(design: flow step 7)*
- ✅ Thrashing guard — rolling window `K`, pause save+restore on >1 switch,
      hysteresis to re-engage. *(design: flow step 8)*

**Done when:** a conv saves at its turn boundary; eviction respects both caps; the
guard pauses under A/B/A/B thrash.

### Phase 4 — Restore

Goal: reload a conversation from disk exactly when it isn't in the live tree.

- ✅ `ram_since_restore` tracking per slot (the action log). *(design: Desk state)*
- ✅ Restore trigger — conv ∉ `ram_since_restore`, before forwarding.
      *(design: event table — RESTORE)*
- ✅ Drain before switch — wait `in_flight == 0` (restore lands on an empty slot).
      *(design: Operations — Drain)*
- ✅ Save displaced conv first — preserve the slot's current conv before restore.
      *(design: Desk state — pseudocode; Slot awareness — Allocation)*
- ✅ 400 handling — bad/old file → delete + continue cold. *(design: Failure modes)*
- ✅ Invalidation events — router restart (all desks) / child unload (per-model) /
      proxy restart (all cold). *(design: Desk state — Invalidation events)*
- ✅ `erase` action — optional tool to force a known-empty slot. *(design: Primitives)*

**Done when:** A→B→A wastes no restore; a server restart lazy-restores on first
switch; a corrupt file 400s to cold.

### Phase 5 — Slot allocator

Goal: the proxy authoritatively assigns convs to slots.

- ✅ `id_slot` injection into every chat body (always on). *(design: Slot awareness)*
- ✅ conv → slot table per model. *(design: Desk state)*
- ✅ Allocation — new conv, all slots held → LRU conv's slot; save displaced first.
      *(design: Slot awareness — Allocation)*

**Done when:** the proxy knows every conv's slot with zero guessing; single-slot is
the one-row degenerate case; multi-slot allocates correctly.

### Phase 6 — Shifted-suffix (compaction / head change / middle edit)

Goal: detect rewrites, prune dead files, slide KV.

- ⬜ Tail-match detection on the new-conv path (overlap ≥ `tail_match_min`).
      *(design: shifted-suffix — Detection threshold)*
- ⬜ Delete the old conv's file on detection (provably dead). *(design: flow step 3)*
- ⬜ `n_cache_reuse` injection — targeted mode (only on the detected request).
      *(design: shifted-suffix — n_cache_reuse)*

**Done when:** a compaction **and** a cwd-move both delete the old file and inject
`n_cache_reuse` on that one request.

### Phase 7 — Robustness

Goal: the proxy's own failures degrade to cold, never down.

- ✅ Control-call timeouts (`control_timeout_ms`) → skip save / delete+cold.
      *(design: Operations — Control-call failures)*
- ✅ Connection drop → fail fast 502 + mark all desks empty. *(design: Operations)*
- ✅ `input_tokens` failure → retry once → degraded pass-through + desk dirty.
      *(design: Operations)*
- ✅ Health polling (`health_poll_ms`) for router-restart detection.
      *(design: Desk state — Lifecycle)*
- ✅ API key carried on control calls. *(design: Operations — API key)*

**Done when:** a server restart is detected via the health poll; every control-call
failure degrades to cold without blocking chat.

### Phase 8 — Observability / Miss debugger

Goal: every miss is diagnosable from logs.

- ✅ Structured logs at INFO / DEBUG / TRACE. *(design: Miss debugger — Log levels)*
- ✅ Per-request correlation record — proxy verdict + server verdict
      (`n_prompt_tokens_cache`, `timings.cache_*`) + actions. *(design: Miss debugger)*
- ✅ Miss taxonomy classification (6 buckets). *(design: Miss debugger)*
- ✅ Debug artifacts — JSON per new-conv event (divergence window, hashes),
      rotated + bounded. *(design: Miss debugger — Debug artifacts)*

**Done when:** a full-prefill is fully explained by its log line (which bucket, why).

### Phase 9 — Testing

Goal: the design's test strategy, green.

- ✅ Unit — tail matcher (extend/fork/shifted-suffix), LRU + hysteresis, filename
      sanitization, ledger math, tmp/rename. *(design: Test strategy — Unit)*
- ⬜ Integration — 8 scenarios against a real `llama-server` at the bee fork commit
      (upstream + cherry-pick as cross-check): round-trip, A→B→A no-waste, shifted-suffix
      delete, thrashing, restore-400, proxy restart, drain, idempotency.
      *(design: Test strategy — Integration)*
- ⬜ Property — saves idempotent (≤1 save/restore); eviction never removes an in-slot
      conv; guard hysteresis bounded; **no-strain invariants** (event-driven only /
      save-idle + restore-empty / guard-OFF = zero control calls / serialized control
      calls). *(design: event table — No-strain invariants; Test strategy — Property)*

**Done when:** all unit + integration + property tests pass against the bee fork
commit (upstream + cherry-pick as cross-check).

## Decisions

- ✅ **Runtime/language** — **Python 3.12** (provided `venv/`); structure mirrors
      `skill-sync-script`. *(closed)*
- ✅ **Config** — single `config.json`, self-documenting (`--generate-config`),
      file > default precedence. *(closed — see design Configuration)*
- ✅ **Reference trees** — verified against **both** (not pinned to one): bee fork
      `0ba48c55` and upstream `25747b08` + #25592 + #26004. 11-item checklist: 10/11
      pass on both; item 9 (`prompt_cache_source = "ram"` reporting) is fork-only.
      Phase 9 tests run against the bee fork, with upstream + cherry-pick as a
      cross-check. *(closed — see design Resources & provenance)*

---

## Log

## [2026-09-30] — Session 1
**Task**: Verify the design against both reference trees (bee fork + upstream +
cherry-pick) instead of pinning to one; document the results.
**Changes**:
- `llamacpp-autosave-proxy-design.md` — Resources & provenance now says "verified
  against two reference trees (not pinned to one)"; added the 11-item verification
  table (10/11 pass on both; item 9 fork-only) + the fork-only difference note.
**Lessons**: "Full verification" = the design's own 11-item re-verification
checklist. #26004 cherry-picks cleanly onto master `25747b08`; the KV-tier mechanism
is upstream, the fork only adds the `prompt_cache_source` reporting string.

## [2026-09-30] — Session 2
**Task**: Phase 0 items 1–3 — entry point, flags, config system.
**Changes**:
- `proxycache.py` — root thin wrapper → `src.__main__.main()`.
- `src/__main__.py` — argparse (`--version`, `--config`, `--generate-config`);
  default = serve (loads config; the serve loop is item 5).
- `src/config.py` — single `config.json`; `DEFAULTS` + `DESCRIPTIONS` (14 knobs from
  the design Knobs table); `--generate-config` self-doc; auto-generate if missing;
  file > default precedence.
- `config.json` — auto-created with defaults.
**Lessons**: mirrors skill-sync-script (namespace package, no `__init__.py`;
`load_config` deep-merges the file over defaults). Tested: all 3 flags + override-wins
+ partial-merge-with-defaults.

## [2026-09-30] — Session 3
**Task**: Phase 0 items 4-5 — requirements.txt + transparent forward.
**Changes**:
- `requirements.txt` — pinned deps (aiohttp 3.14.3).
- `src/proxy.py` — transparent forward: catch-all route forwarding every request to
  upstream, responses streamed back byte-identical; 502 when upstream down.
- `src/__main__.py` — serve loop (default = start the proxy on `listen` → `upstream`).
**Lessons**: verified byte-identical with a mock upstream (GET, POST with body/header/
query, streaming in chunks) + 502 on dead upstream. Phase 0 "Done when" gate met;
deployment prerequisites (server-only-via-proxy, shared FS) are operational.

## [2026-09-30] — Session 4
**Task**: Phase 1 — Endpoint routing (route table, desk-dirty, slot count, path layout).
**Changes**:
- `src/routes.py` — route table: classify(method, path, query) → Action (INTERCEPT,
  PASS_DESK_DIRTY, PASS, PROXY_INTERNAL). 42/42 test cases pass against the design's
  endpoint table.
- `src/desk.py` — Desk state: n_slots (from GET /slots), ram_since_restore per slot,
  mark_dirty/mark_all_dirty/is_warm/add_forward/set_restore.
- `src/storage.py` — conv_path(save_root, model, conv) → {root}/{model}/{conv}.bin.
**Lessons**: route table is method-aware (POST /models = desk dirty, GET /models =
pass). Desk-dirty = ram_since_restore ∅ (next chat is a restore candidate).

## [2026-09-30] — Session 5
**Task**: Phase 2 — Conversation keying (input_tokens fingerprint, tail match,
content-derived id, forks, X-Conversation-Id hybrid).
**Changes**:
- `src/convkey.py` — ConvTracker: check(tokens, header_id) -> conv id. (1) header wins
  (sanitized); (2) same-conv = longest-tail prefix match; (3) else new conv =
  derive_id (sha256 of head+tail). Memory-bounded: stores only tail+length per conv.
- `src/fingerprint.py` — get_tokens(session, upstream, body) -> token list via
  POST /chat/completions/input_tokens.
**Lessons**: id = hash(head+tail), NOT just head — else a fork (same head,
different tail) collides with its parent. Same-conv check is probabilistic
(tail match at the expected position), O(tail_len) per conv. Tested: two turns ->
same id, fork -> two ids, header respected+sanitized, derive_id deterministic.

## [2026-09-30] — Session 6
**Task**: Phase 3 — Save (trigger, .tmp cleanup, ledger, LRU, thrashing guard).
**Changes**:
- `src/save.py` — should_save() (trigger condition); delete_stale_tmp() (startup
  cleanup); Ledger (n_written/n_saved + LRU evict with protected in-slot convs);
  ThrashingGuard (rolling window, pause on >1 switch, hysteresis re-engage).
**Lessons**: thrashing guard pauses on >1 switch (threshold=1 means >=2), re-engages
when switches drop to <=hysteresis (0). LRU evict never touches protected (in-slot)
convs. The server does the .tmp+rename; the proxy only deletes stale .tmp at startup.

## [2026-09-30] — Session 7
**Task**: Phase 4 — Restore (trigger, drain, save-first, 400 handling, invalidation).
**Changes**:
- `src/restore.py` — decide() -> (RestoreAction, conv_to_save): FORWARD_WARM (C in
  tree), RESTORE / SAVE_THEN_RESTORE (C not in tree, file exists), FORWARD_COLD
  (no file or in_flight != 0). handle_restore_400() (delete file). invalidate_all/
  invalidate_model (router restart / child unload).
**Lessons**: restore is destructive (clears live tree) -> must drain (in_flight==0)
and save the displaced conv first. 400 = delete file + continue cold (server
full-prefills). Invalidation = mark_all_dirty (all sets = ∅).

## [2026-09-30] — Session 8
**Task**: Phase 5 — Slot allocator (id_slot injection, conv->slot table, allocation).
**Changes**:
- `src/allocator.py` — inject_id_slot(body, slot) (always on, client never sees it);
  SlotAllocator: allocate(conv, lru_order) -> (slot, conv_to_evict). Reuses existing
  slot, finds free slot, or evicts LRU conv's slot when all held.
**Lessons**: single-slot is the one-row degenerate case (n_slots=1). id_slot is
injected into the JSON body (server-accepted field, client-transparent).

## [2026-09-30] — Session 9
**Task**: Phase 6 — Shifted-suffix (tail-match detection, delete old file, n_cache_reuse).
**Changes**:
- `src/shifted.py` — detect_shifted_suffix(new, old, tail_match_min) -> (is_shifted,
  overlap): checks if new's suffix matches old's suffix (largest overlap >=
  tail_match_min). should_inject_n_cache_reuse(is_shifted, targeted_mode).
**Lessons**: shifted-suffix = suffix unchanged, head shifted (compaction / head
change / middle edit). Detection is a SUFFIX match (not prefix) — the tail is
unchanged, the head moved. n_cache_reuse is targeted (only on the detected
request) -> zero cost when unneeded.

## [2026-09-30] — Session 10
**Task**: Phase 7 — Robustness (timeouts, connection drop, input_tokens retry, health poll).
**Changes**:
- `src/robustness.py` — safe_parse_body (None on fail -> pass through);
  handle_control_call_failure (save->skip_save, restore->delete_cold);
  TimeoutConfig + should_timeout; on_mid_stream_disconnect; on_connection_drop
  (mark_all_dirty); on_input_tokens_failure (retry once -> degraded);
  HealthPoller (should_poll).
**Lessons**: every control-call failure degrades to cold, never down. Body parse
fail -> pass through unmodified (never drop the request). input_tokens failure ->
retry once, then degraded pass-through + desk dirty. Health poll detects router
restart.

## [2026-09-30] — Session 11
**Task**: Phase 8 — Observability (miss classification, metrics, X-Conversation-Id echo).
**Changes**:
- `src/observability.py` — MissReason enum (NO_FILE/RESTORE_400/BELOW_MIN/
  THRASH_PAUSED/COLD); RequestMetrics (n_prompt/n_kv_self/n_ctx/n_saved/n_written);
  classify_miss() -> MissReason; echo_conversation_id(); log_ram_since_restore().
**Lessons**: a full-prefill is fully explained by its log line (which bucket, why).
Miss taxonomy: NO_FILE (no file), RESTORE_400 (bad file), BELOW_MIN (L < min),
THRASH_PAUSED (guard paused), COLD (forward cold).

## [2026-09-30] — Session 12
**Task**: Phase 9 — Testing (unit test runner).
**Changes**:
- `tests/run_all.py` — unified test runner: runs all 9 phase tests (config, routes,
  convkey, save, restore, allocator, shifted, robustness, observability). 9 passed,
  0 failed.
**Lessons**: integration + property tests (Phase 9 items 2-3) require a real
llama-server at the bee fork commit — not runnable in this environment. Unit tests
cover the "Done when" gates for each phase.
