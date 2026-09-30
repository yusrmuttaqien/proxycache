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
| 0 | Scaffolding | ⬜ | entry point, config (load/generate), transparent forward |
| 1 | Endpoint routing | ⬜ | route table, desk-dirty, slot count, `{model}/{conv}.bin` |
| 2 | Conversation keying | ⬜ | content-derived id, tail match, forks, sanitize |
| 3 | Save | ⬜ | trigger, tmp+rename, ledger, LRU, thrashing guard |
| 4 | Restore | ⬜ | trigger, drain, idempotent, invalidation, `erase` |
| 5 | Slot allocator | ⬜ | `id_slot`, conv→slot, allocation |
| 6 | Shifted-suffix | ⬜ | detect+delete, `n_cache_reuse` |
| 7 | Robustness | ⬜ | 400/cold, degraded, timeouts, conn drop, health poll |
| 8 | Observability / Miss debugger | ⬜ | taxonomy, artifacts, log levels |
| 9 | Testing | ⬜ | unit, integration, property |

Status legend: ⬜ not started · 🔨 in progress · ✅ done (+tested)

---

## Work queue (detailed, per phase)

Each task cites the **design section** it implements
(`llamacpp-autosave-proxy-design.md`). Check a box only when the task lands **and** is
tested. Phases are ordered so each is independently runnable.

### Phase 0 — Scaffolding

Goal: a config-driven transparent reverse proxy that forwards a chat request
byte-identically. **Structure mirrors `skill-sync-script`.**

- ⬜ Entry point — `proxycache.py` (thin wrapper) → `src/__main__.py`
      (`argparse`, `main() -> int`) → `src/config.py`. Run:
      `venv/bin/python proxycache.py [flags]`. *(mirrors skill-sync-script)*
- ⬜ Flags — `--version`, `--config <path>`, `--generate-config`. **No TUI**: default
      (no flags) = **serve the proxy**. *(design: Configuration)*
- ⬜ Config system — single `config.json`; `--generate-config` emits every key with
      default + accepted values + description (self-documenting, skillsync-style);
      auto-generate with defaults if missing; precedence file > default.
      *(design: Configuration — Knobs table)*
- ⬜ `requirements.txt` — pinned deps; use the provided `venv/` (Python 3.12).
- ⬜ Transparent forward — all requests proxied, responses streamed back
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

- ⬜ Route table — intercept chat endpoints (full flow); pass-through the rest.
      *(design: Transparency — endpoint table)*
- ⬜ Desk-dirty marking — non-chat slot mutators (completion/embedding/rerank/
      slots-no-action/models-lifecycle/stream) invalidate `ram_since_restore`.
      *(design: Transparency — Desk dirty)*
- ⬜ Slot count read at startup per model (`GET /slots`). *(design: Slot awareness)*
- ⬜ **Filename namespacing** — `{model}/{conv}.bin` (per-model subdirs). *(design:
      Deployment; Resolved decisions — Multi-model)*

**Done when:** chat is intercepted; an `/embeddings` call marks the desk dirty;
slot count is known; files land under `{model}/`.

### Phase 2 — Conversation keying

Goal: identify conversations with zero client cooperation.

- ⬜ `input_tokens` fingerprint per chat request. *(design: Conversation key)*
- ⬜ Memory-bounded parse — keep head (root exchange) + tail (match window),
      drop the middle. *(design: Conversation key — Memory-bounded parse)*
- ⬜ Same-conversation check — longest-tail prefix match. *(design: Conversation key)*
- ⬜ New conversation — content-derived id (hash of root exchange); deterministic.
      *(design: Conversation key)*
- ⬜ Fork handling — branch-split when two convs extend the same tail then diverge.
      *(design: Conversation key — Forks)*
- ⬜ `X-Conversation-Id` hybrid — header wins when present; sanitize to
      `[A-Za-z0-9._-]`. *(design: Conversation key — Known limitations)*

**Done when:** two turns of a conv → same id; a fork → two ids; a declared header id
is respected and sanitized.

### Phase 3 — Save

Goal: persist a conversation's slot to disk at the right moments, bounded.

- ⬜ Save trigger — response fully received AND guard ON AND `L ≥ min_save_tokens`
      AND conv in slot table. *(design: event table — SAVE; flow step 6)*
- ⬜ Turn boundary includes **client disconnect mid-stream** (save the partial state).
      *(design: Operations #2)*
- ⬜ Save failure (disk full / 500) → log + skip this turn; retry next turn
      (idempotent, latest wins). *(design: Operations #3)*
- ⬜ `.tmp` + rename — atomic write; delete stale `.tmp` at startup.
      *(design: Failure modes)*
- ⬜ Ledger — `n_written`/`n_saved` accounting for exact sizes. *(design: flow step 6)*
- ⬜ LRU eviction — `n_max_files` / `max_bytes`; evict oldest-used, never an
      in-slot conv. *(design: flow step 7)*
- ⬜ Thrashing guard — rolling window `K`, pause save+restore on >1 switch,
      hysteresis to re-engage. *(design: flow step 8)*

**Done when:** a conv saves at its turn boundary; eviction respects both caps; the
guard pauses under A/B/A/B thrash.

### Phase 4 — Restore

Goal: reload a conversation from disk exactly when it isn't in the live tree.

- ⬜ `ram_since_restore` tracking per slot (the action log). *(design: Desk state)*
- ⬜ Restore trigger — conv ∉ `ram_since_restore`, before forwarding.
      *(design: event table — RESTORE)*
- ⬜ Drain before switch — wait `in_flight == 0` (restore lands on an empty slot).
      *(design: Operations — Drain)*
- ⬜ Save displaced conv first — preserve the slot's current conv before restore.
      *(design: Desk state — pseudocode; Slot awareness — Allocation)*
- ⬜ 400 handling — bad/old file → delete + continue cold. *(design: Failure modes)*
- ⬜ Invalidation events — router restart (all desks) / child unload (per-model) /
      proxy restart (all cold). *(design: Desk state — Invalidation events)*
- ⬜ `erase` action — optional tool to force a known-empty slot. *(design: Primitives)*

**Done when:** A→B→A wastes no restore; a server restart lazy-restores on first
switch; a corrupt file 400s to cold.

### Phase 5 — Slot allocator

Goal: the proxy authoritatively assigns convs to slots.

- ⬜ `id_slot` injection into every chat body (always on). *(design: Slot awareness)*
- ⬜ conv → slot table per model. *(design: Desk state)*
- ⬜ Allocation — new conv, all slots held → LRU conv's slot; save displaced first.
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

- ⬜ Control-call timeouts (`control_timeout_ms`) → skip save / delete+cold.
      *(design: Operations — Control-call failures)*
- ⬜ Connection drop → fail fast 502 + mark all desks empty. *(design: Operations)*
- ⬜ `input_tokens` failure → retry once → degraded pass-through + desk dirty.
      *(design: Operations)*
- ⬜ Health polling (`health_poll_ms`) for router-restart detection.
      *(design: Desk state — Lifecycle)*
- ⬜ API key carried on control calls. *(design: Operations — API key)*

**Done when:** a server restart is detected via the health poll; every control-call
failure degrades to cold without blocking chat.

### Phase 8 — Observability / Miss debugger

Goal: every miss is diagnosable from logs.

- ⬜ Structured logs at INFO / DEBUG / TRACE. *(design: Miss debugger — Log levels)*
- ⬜ Per-request correlation record — proxy verdict + server verdict
      (`n_prompt_tokens_cache`, `timings.cache_*`) + actions. *(design: Miss debugger)*
- ⬜ Miss taxonomy classification (6 buckets). *(design: Miss debugger)*
- ⬜ Debug artifacts — JSON per new-conv event (divergence window, hashes),
      rotated + bounded. *(design: Miss debugger — Debug artifacts)*

**Done when:** a full-prefill is fully explained by its log line (which bucket, why).

### Phase 9 — Testing

Goal: the design's test strategy, green.

- ⬜ Unit — tail matcher (extend/fork/shifted-suffix), LRU + hysteresis, filename
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

## [2026-01-08] — Session 1
**Task**: Verify the design against both reference trees (bee fork + upstream +
cherry-pick) instead of pinning to one; document the results.
**Changes**:
- `llamacpp-autosave-proxy-design.md` — Resources & provenance now says "verified
  against two reference trees (not pinned to one)"; added the 11-item verification
  table (10/11 pass on both; item 9 fork-only) + the fork-only difference note.
**Lessons**: "Full verification" = the design's own 11-item re-verification
checklist. #26004 cherry-picks cleanly onto master `25747b08`; the KV-tier mechanism
is upstream, the fork only adds the `prompt_cache_source` reporting string.
