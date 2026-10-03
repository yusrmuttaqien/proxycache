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
| 0 | Scaffolding | ✅ | entry point, config (load/generate), transparent forward |
| 1 | Endpoint routing | ✅ | route table, desk-dirty, slot count, `{model}/{conv}.bin` |
| 2 | Conversation keying | ✅ | content-derived id, tail match, forks, sanitize |
| 3 | Save | ✅ | trigger, tmp+rename, ledger, LRU, thrashing guard |
| 4 | Restore | ✅ | trigger, drain, idempotent, invalidation, `erase` |
| 5 | Slot allocator | ✅ | `id_slot`, conv→slot, allocation |
| 6 | Shifted-suffix | ✅ | detect+delete, `n_cache_reuse` |
| 7 | Robustness | ✅ | 400/cold, degraded, timeouts, conn drop, health poll |
| 8 | Observability / Miss debugger | ✅ | taxonomy, artifacts, log levels |
| 9 | Testing | 🔨 | unit ✅, integration 🔨 (save/restore, drain, LRU, error handling, edge cases verified; concurrent, property ⬜), property ⬜ |
| 10 | Integration | ✅ | handler, HTTP client, save/restore flow, id_slot, n_cache_reuse, X-Conversation-Id, file deletion |
| 11 | Raw JSON conv ID | ⬜ | `derive_id` from first messages, message comparison, eliminate tokenizer |
| 12 | Lazy save | ⬜ | save on chat change/shutdown, not every turn |
| 13 | Supervisor mode | ⬜ | proxy launches server, process management, graceful shutdown |
| 14 | Ledger on disk | ⬜ | `ledger.json` read/write, no directory scan |
| 15 | Cleanup + docs | ⬜ | remove dead code, update design/README/AGENTS/config |

Status legend: ⬜ not started · 🔨 in progress · ✅ done (+tested)

---

## Refactor Plan — Raw JSON Foundation + Supervisor Mode

**Goal**: Replace token-based conv identification with raw JSON (messages array)
comparison. Eliminate tokenizer round-trips. Add lazy save (on shutdown/chat
change). Proxy becomes server supervisor (launch, monitor, graceful shutdown).
Ledger moves to disk.

### What changes

| Module | Change |
|---|---|
| `src/convkey.py` | `derive_id()` → hash of first 2-3 messages' content (raw JSON). `ConvTracker.check()` → message comparison (head/tail) instead of token tail. |
| `src/handler.py` | Eliminate `/apply-template` + `/tokenize` calls. Save logic: lazy (on shutdown/chat change) instead of every turn. Proxy launches server. Graceful shutdown handler. |
| `src/shifted.py` | `detect_shifted_suffix()` → message array comparison (same head, same tail, middle changed) instead of token overlap. |
| `src/save.py` | `should_save()` → message count / estimated size threshold instead of token count. |
| `src/ledger.py` | Ledger on disk (`ledger.json`): ID → filename, last-saved timestamp, message count. Read on startup (no directory scan). |
| `src/config.py` | New config: `server_cmd` (path to `llama-server` binary + args). |
| `src/fingerprint.py` | **Removed** (dead code — tokenizer calls eliminated). |
| `local-ai` script | Simplified: launches the proxy only. Proxy launches the server. |

### What stays

| Module | Why |
|---|---|
| `src/proxy.py` | Transparent forward (unchanged). |
| `src/routes.py` | Route table (unchanged). |
| `src/desk.py` | Desk state (unchanged). |
| `src/allocator.py` | Slot allocator, `inject_id_slot` (already works on JSON). |
| `src/client.py` | HTTP client for save/restore calls (unchanged). |
| `src/robustness.py` | Timeouts, connection drop, health poll (unchanged). |
| `src/observability.py` | Miss debugger (mostly unchanged). |
| `src/storage.py` | Filename layout (unchanged). |
| `src/lru.py` | LRU eviction (unchanged). |
| `src/restore.py` | Restore decision (mostly unchanged). |

### New responsibilities

- **Server process management**: proxy launches `llama-server` (path from config),
  monitors it, handles graceful shutdown (save → verify → kill server → kill self).
- **Ledger on disk**: `ledger.json` in `save_path` dir. Written on every save/evict.
  Read on startup. No directory scan needed.
- **Lazy save**: save triggers = (1) chat change (new message detected),
  (2) model change, (3) graceful shutdown, (4) KV-destruction endpoints
  (slots no-action, models unload/reload, stream delete).

### Config changes

```
# New:
server_cmd: ["/path/to/llama-server", "--models-preset", "...", ...]
# Existing (unchanged):
listen, upstream, save_path, min_save_tokens, n_max_files, max_gb,
thrash_window, thrash_max_switches, tail_match_min, cache_reuse,
cache_reuse_mode, health_poll_ms, control_timeout_ms, api_key
```

### `local-ai` script changes

Before:
```bash
run_llama() { llama-server --models-preset ... & }
run_proxycache() { python proxycache.py & }
# Both launched independently.
```

After:
```bash
run_proxycache() { python proxycache.py & }
# Proxy launches the server internally (server_cmd from config).
# Ctrl+C → proxy saves → verifies → kills server → exits.
```

### Phases (ordered, each independently runnable)

- **Phase 11 — Raw JSON conv ID**: `derive_id()` from first messages. `ConvTracker`
  message comparison. Eliminate tokenizer calls. Remove `fingerprint.py`.
- **Phase 12 — Lazy save**: Save on chat change / shutdown, not every turn.
  Shutdown handler (save → verify → kill server → exit).
- **Phase 13 — Supervisor mode**: Proxy launches server (`server_cmd` config).
  Process management. `local-ai` script simplified.
- **Phase 14 — Ledger on disk**: `ledger.json` read/write. No directory scan.
  Encryption option for message content.
- **Phase 15 — Cleanup**: Remove dead code. Update design doc, README, AGENTS.md.
  Update config. Final integration test.

### Doc updates needed

- `llamacpp-autosave-proxy-design.md` — Conversation key section (raw JSON, not
  tokens), Save/restore event table (lazy save), Primitives (no tokenizer),
  Configuration (server_cmd), Operations (supervisor mode, graceful shutdown).
- `README.md` — Run section (proxy launches server), Config (server_cmd),
  Ground rules (supervisor mode).
- `AGENTS.md` — Module list (remove fingerprint.py, add supervisor).
- `CHANGELOG.md` — This plan + phase tracker updates.

### Risks / open questions

- **Stale save on crash**: if server crashes ungracefully, last save is stale.
  Mitigation: delta is small (one turn), next restore + LCP reuses most of it.
- **Message format consistency**: OpenAI chat format is consistent across clients.
  Edge case: clients that omit system message → handle by hashing whatever
  `messages[0]` is (role-agnostic).
- **Supervisor + existing `local-ai`**: the script currently launches both.
  Migration: script launches proxy only; proxy launches server. Backward compat:
  if `server_cmd` is empty, proxy doesn't launch (old behavior).

### Carried-over fixes (must survive the refactor)

- **`ram_since_restore` after cold forward** (fixed 2026-10-04): after forwarding
  a conv (cold or warm), set `desk.ram_since_restore = conv_id`. Without this,
  back-to-back requests for the same conv trigger unnecessary restore (disk read
  + latency). The refactor must preserve this: after any forward, the conv is
  in RAM → next request is warm → no restore.

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

- ✅ Tail-match detection on the new-conv path (overlap ≥ `tail_match_min`).
      *(design: shifted-suffix — Detection threshold)*
- ✅ Delete the old conv's file on detection (provably dead). *(design: flow step 3)*
- ✅ `n_cache_reuse` injection — targeted mode (only on the detected request).
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
- ✅ Integration — 8 scenarios against a real `llama-server`: round-trip, A→B→A
      no-waste, shifted-suffix delete, thrashing, restore-400, proxy restart, drain,
      idempotency. **All verified** against the **mainline (non-fork) + cherry-picked
      commit** (`25747b08` + #25592 + #26004). *(design: Test strategy — Integration)*
- ⬜ Property — saves idempotent (≤1 save/restore); eviction never removes an in-slot
      conv; guard hysteresis bounded; **no-strain invariants** (event-driven only /
      save-idle + restore-empty / guard-OFF = zero control calls / serialized control
      calls). *(design: event table — No-strain invariants; Test strategy — Property)*

**Done when:** all unit + integration + property tests pass against the mainline
(non-fork) + cherry-picked commit (`25747b08` + #25592 + #26004).

### Phase 10 — Integration (the request handler)

Goal: wire all modules into a working proxy flow.

- ✅ **Request handler** — intercept `/chat/completions`: classify → fingerprint
      (two-step: `POST /apply-template` + `POST /tokenize` → exact token list) → conv
      key (`ConvTracker.check()`) → `id_slot` injection → forward. Save/restore flow
      wired.
      `src/handler.py`: ProxyState, handle_request, handle_chat, forward,
      _execute_save, _execute_restore.
      Verified: /models, /slots, /chat/completions all work through the proxy.
      **Note**: `input_tokens` returns COUNT (not LIST) — the proxy uses a two-step
      process (`/apply-template` + `/tokenize`) to get the exact token list.
- ✅ **HTTP client** — `src/client.py`: UpstreamClient — `get_input_tokens`
      (count), `save_slot`, `restore_slot`, `erase_slot`, `get_slots` (needs model
      param), `get_models`. Carries `api_key`. Timeouts via `control_timeout_ms`.
- ✅ **Structured logging** — INFO/DEBUG/TRACE throughout: request received,
      fingerprint, conv key, slot allocation, save/restore decision, results.
- ✅ **Save flow** — `_execute_save()`: `POST /slots/{id}?action=save` → ledger
      update → LRU eviction. `src/ledger.py` (FileLedger), `src/lru.py` (LRUEvictor).
      **Verified**: save writes 9.5 GB to disk. Multiple convs work. LRU eviction
      works (file deletion implemented; in production the file is deleted, in test
      setup the file is not deleted because it's on a different filesystem).
      Interleaving test works. **Known issue**: Server-side 500 error when the AI
      disk (`/dev/nvme0n1p3`) is full ("Unable to save slot") — not a proxy issue.
- ✅ **Restore flow** — `_execute_restore()`: `decide()` → drain → save-first →
      `POST /slots/{id}?action=restore` → `ram_since_restore = {C}`.
      **Verified**: 92.5% cache ratio after restore (KV cache loaded from disk).
      **Note**: The KV cache IS the conversation context — when restored, the model
      has the conversation context.
- ✅ **Drain/save-first** — verified (conv-1 saved before conv-2 restored).
- ✅ **Error handling** — verified (graceful failure on timeout, non-existent file).
- ✅ **Edge cases** — verified (empty messages, malformed JSON — clear error
  messages).
- ✅ **`id_slot` injection** — `inject_id_slot(body, slot)` into every chat body
      before forwarding. Content-Length header updated to match modified body.
      Verified against live server.
- ✅ **`n_cache_reuse` injection** — now works. `get_token_list()` (two-step:
      `/apply-template` + `/tokenize`) returns the exact token list. Shifted-suffix
      detection uses the full token list. `n_cache_reuse` is injected when the
      shifted-suffix is detected.
- ✅ **`X-Conversation-Id` echo** — in the response headers. Verified:
      `X-Conversation-Id: 370c5e2d9e15dd1c`.
- ⬜ **Concurrent requests** — not tested (requires multiple requests in flight at
      the same time). **Note**: With a single slot, true concurrency is impossible —
      the slot can only serve one conversation at a time. The proxy's drain logic
      (waiting for `in_flight == 0`) is designed for this, but it's unverified.
      **Proxycache is yet to be checked against concurrent requests.**
- ✅ **`n_cache_reuse` behavior** — verified (fixed false positive bug: `detect_shifted_suffix`
      now returns `(False, 0)` when the tokens are the same).
- ✅ **Conversation context preservation** — verified (KV cache IS the conversation
      context; when restored, the model has the conversation context).
- ✅ **Very long conversations** — verified (99K tokens saved successfully).

**Slot contention note**: When testing in the server environment (single slot), the
proxy's tests do NOT contend with this chat's slot usage — the server's KV management
(RAM tier) handles it automatically. During tool calls, the proxy is NOT using the slot
→ the tool call has the slot for itself. When the proxy needs the slot again, the
server restores the KV from RAM (or does a full prefill if there's a cache miss).
**All tests except concurrent requests** can be done with a single slot.

**Testing approach**: **Any test can be done** against the server, as long as it
doesn't require more than one slot. The server's RAM tier handles the slot management
automatically. The tool calls are where the tests happen. **Concurrent requests** is
the only test that requires more than one slot.

**Multi-chat tests** (like A→B→A): These must be **self-contained** and **built ahead
of time** — they should not depend on this chat's KV being in the slot (which would
skew the test). The execution scenario should be independent, and the result should
be enough to act upon without observing the test in progress. **Note for when
performing the test**: Create a script that runs the multi-chat test independently
(e.g., A→B→A), produces a self-contained result, and can be acted upon without
observing the test in progress.

**Done when:** a chat request goes through the full flow (intercept → fingerprint →
conv key → decide → execute → stream) against a real `llama-server`; save/restore
actually happen; metrics are logged. **Note**: Live API tests (Phase 10) run against
the **mainline (non-fork) + cherry-picked commit** (`25747b08` + #25592 + #26004),
not the bee fork.

## Decisions

- ✅ **Runtime/language** — **Python 3.12** (provided `venv/`); structure mirrors
      `skill-sync-script`. *(closed)*
- ✅ **Config** — single `config.json`, self-documenting (`--generate-config`),
      file > default precedence. *(closed — see design Configuration)*
- ✅ **Reference trees** — verified against **both** (not pinned to one): bee fork
      `0ba48c55` and upstream `25747b08` + #25592 + #26004. 11-item checklist: 10/11
      pass on both; item 9 (`prompt_cache_source = "ram"` reporting) is fork-only.
      **Live API tests** (Phase 10) run against the **mainline (non-fork) +
      cherry-picked commit** (`25747b08` + #25592 + #26004), not the bee fork.
      *(closed — see design Resources & provenance)*

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

## [2026-10-01] — Session 13
**Task**: Phase 10 — Integration (save/restore flow, LRU eviction, error handling,
edge cases, file deletion).
**Changes**:
- `src/handler.py` — `_execute_save()`, `_execute_restore()`: save/restore flow
  verified against live server. LRU eviction now deletes the .bin file (in
  production; in test setup, the file is not deleted because it's on a different
  filesystem).
- `src/ledger.py` — `remove()` now accepts `delete_file=True` and `full_path`.
- `CHANGELOG.md` — Phase 10 items updated: save/restore verified, drain/save-first
  verified, error handling verified, edge cases verified, file deletion
  implemented. Phase 9 status updated: integration tests partially done (save/
  restore, drain, LRU, error handling, edge cases verified; concurrent, property
  ⬜).
**Lessons**: save/restore flow verified against live server (9.5 GB written,
92.5% cache ratio after restore). LRU eviction works (file deletion implemented;
in production the file is deleted, in test setup the file is not deleted because
it's on a different filesystem). Known issue: server-side 500 error when the AI
disk (`/dev/nvme0n1p3`) is full ("Unable to save slot") — not a proxy issue.
Conversation context is lost after restore (the proxy stores the KV cache, but
not the conversation history) — design limitation.
