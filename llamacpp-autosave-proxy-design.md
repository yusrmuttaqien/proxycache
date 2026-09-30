# llama.cpp Slot Auto-Save Proxy — Design (Draft 3)

Goal: a proxy in front of `llama-server` (beellama.cpp fork) that auto-persists slot KV state
to disk so conversations survive server restarts. Constraints: **zero client-side
config/plugins**, reliable (no wasted restores), light on storage.

**Deployment**: single user today, `llama-server` in **router mode** (easy model swap),
limited to **one slot per model** for now. Must scale to **multi-slot / multi-model**
when resources grow. ⇒ **Key everything by model**: one desk per model, filenames
namespaced `{model}/{conv}.bin`. Single-user = one hot model; the same code path tracks
more desks as models/users are added. No redesign.

## Resources & provenance

All server behavior in this doc was verified against **real source**, not docs, and is
**verified against two reference trees** (not pinned to one):

- **Bee fork** (`https://github.com/Anbeeld/beellama.cpp`), commit
  `0ba48c55a17cb4d3f5f1bd76bba8ee52fdb2573f` (2026-09-25, "Merge branch 'v0.4.7'") —
  the fork the user runs.
- **Upstream** (`ggml-org/llama.cpp`), master `25747b08` + cherry-picks
  **#25592** (hybrid/recurrent `pos_min=pos_max` restore fix) + **#26004**
  (`SCKP` checkpoint appendix). The KV-tier mechanism (`server_prompt_cache`,
  `prompt_save`/`prompt_load`, `cache_idle_slots`) is already in upstream master; the
  two cherry-picks add the checkpoint logic the hybrid/recurrent model needs. `#26004`
  cherry-picks cleanly onto `25747b08` (auto-merge, no conflicts); `#25592` is the same
  kind of additive checkpoint change.

**Verification — 11-item checklist, run against both trees:**

| # | Check | Bee `0ba48c55` | Upstream `25747b08` + #25592 + #26004 |
|---|---|---|---|
| 1 | save/restore actions | ✓ | ✓ |
| 2 | `n_saved`/`n_written` fields | ✓ | ✓ |
| 3 | route table | ✓ | ✓ |
| 4 | `input_tokens` fingerprint | ✓ | ✓ |
| 5 | `n_cache_reuse` per-request | ✓ | ✓ |
| 6 | embedding/rerank slot-based | ✓ | ✓ |
| 7 | slot pinning/reporting (`id_slot`) | ✓ | ✓ |
| 8 | erase action | ✓ | ✓ |
| 9 | RAM-tier auto-restore reporting (`prompt_cache_source = "ram"`) | ✓ | **✗ fork-only** |
| 10 | slot-selection ladder | ✓ | ✓ |
| 11 | queue deferral | ✓ | ✓ |

**One fork-only difference (item 9):** upstream has the RAM-tier *mechanism*
(`prompt_save`/`prompt_load`, `cache_idle_slots`) but not the fork's
`prompt_cache_source = "ram"` *reporting string*. The proxy's miss-debugger
`cache_source` bucket reads that string; against upstream it must derive the source
from a different signal (e.g. presence of a checkpoint / `n_cache_reuse`), not the
fork's string. Every other primitive the proxy drives is identical in both trees.

**Key source references (claim → location):**

| Claim in this doc | Location (at the bee fork commit `0ba48c55`; line numbers drift on upstream) |
|---|---|
| Route table (all endpoints) | `tools/server/server.cpp` (~lines 260–340) |
| `action=save` / `restore` / **`erase`** dispatch; 400 without `--slot-save-path` | `tools/server/server-context.cpp` ~5543–5570; `handle_slots_erase` ~6141 |
| Save/restore response JSON (`n_saved`, `n_written`, `timings`) | `tools/server/server-task.cpp` — `server_task_result_slot_save_load` (~line 1711) |
| `n_cache_reuse` per-request field + base default | `tools/server/server-schema.cpp` ~86, ~583 |
| KV shift mechanics (`seq_add(..., kv_shift)`) | `tools/server/server-context.cpp` ~3766–3810 |
| Embedding/rerank use the slot pool | `tools/server/server-context.cpp` ~4517 (`SERVER_TASK_TYPE_EMBEDDING`/`RERANK`) |
| **Three KV tiers**; RAM tier auto-restore (`prompt_cache_source = "ram"`) | `tools/server/server-context.cpp` ~423 (`prompt_load`), ~1598–1606 (`--cache-idle-slots`) |
| **Slot-selection ladder**: pinned id → restorable-prefix → LRU; auto-save displaced slot (<0.5 keep) | `tools/server/server-context.cpp` ~1730–1835 |
| **`id_slot` request pin** (body field) | `tools/server/server-context.cpp:5089` (`json_value(data, "id_slot", -1)`) |
| **`id_slot` in response** | `tools/server/server-context.cpp:2448` (`res->id_slot = slot.id`) |
| **Queue deferral** (no preemption, KV-neutral wait) | `tools/server/server-context.cpp:2847` ("no slot is available, defer task"); `tools/server/server-queue.h` (`queue_tasks_deferred`) |
| **No slot idle-release timer** (verified absence) | `tools/server/server-context.cpp` — no idle-timeout param; slot states only (`SLOT_STATE_IDLE` ~143) |
| **Per-response reuse report**: `n_prompt_tokens_cache` + `timings.{cache_lcp_n, cache_planned_n, cache_reprocessed_n, cache_source, cache_reason}` | `tools/server/server-common.h` ~317, ~404–415; set at `server-context.cpp` 2423/2472 |

**Re-verification checklist** (run after any fork/upstream version change, before trusting
this doc or the implementation):

```sh
git clone --depth 1 https://github.com/Anbeeld/beellama.cpp && cd beellama.cpp
git rev-parse HEAD   # compare against the pinned commit above
# 1. Slot actions still exist and gated on --slot-save-path:
grep -n 'action == "save"\|action == "restore"' tools/server/server-context.cpp
# 2. Response fields unchanged:
grep -n 'n_saved\|n_written' tools/server/server-task.cpp
# 3. Route table — diff against the endpoint map in this doc:
grep -n 'ctx_http\.' tools/server/server.cpp
# 4. Fingerprint endpoint still compute-free:
grep -n 'input_tokens' tools/server/server.cpp
# 5. n_cache_reuse still a per-request field:
grep -n 'n_cache_reuse' tools/server/server-schema.cpp
# 6. Embedding/rerank still slot-based:
grep -n 'SERVER_TASK_TYPE_EMBEDDING\|SERVER_TASK_TYPE_RERANK' tools/server/server-context.cpp
# 7. Slot pinning (request) + slot reporting (response):
grep -n 'json_value(data, "id_slot"' tools/server/server-context.cpp
grep -n 'res->id_slot' tools/server/server-context.cpp
# 8. Erase action still exists:
grep -n 'action == "erase"' tools/server/server-context.cpp
# 9. RAM tier auto-restore still present:
grep -n 'prompt_cache_source = "ram"' tools/server/server-context.cpp
# 10. Slot-selection ladder (pinned / restorable prefix / LRU):
grep -n 'selected slot by' tools/server/server-context.cpp
# 11. Queue deferral (no preemption):
grep -n 'no slot is available, defer task' tools/server/server-context.cpp
```

If **either** reference tree moves (fork rebase, upstream master advance, or a cherry-pick
amended), re-run this checklist against both and update the file:line table — line numbers
drift, the grep anchors are the stable references.

## Primitives (from the server)

- `POST /slots/{id}?action=save` body `{"filename": "..."}` → writes whole-slot KV + prompt
  tokens to `--slot-save-path/<filename>`. **Defers until the slot is idle.**
  Response: `{id_slot, filename, n_saved, n_written, timings:{save_ms}}`.
- `POST /slots/{id}?action=restore` body `{"filename": "..."}` → clears slot, loads file,
  validates tokens + `n_ctx` fit. 400 on bad file / ctx too small.
- `POST /slots/{id}?action=erase` → clears the slot **without** loading a file
  (verified: `handle_slots_erase`). Optional tool — e.g. force a known-empty state.
- **Three KV tiers** (verified): (1) **live slot tree** (VRAM) — cleared by restore;
  (2) **RAM prompt-cache** (`--cache-ram`/`--cache-idle-slots`) — *separate structure*,
  **NOT cleared by restore**, server-automatic: on every task start the server tries
  it before prefill (`prompt_cache_source = "ram"`, `server-context.cpp:423`);
  (3) **disk save** — the proxy's tier. Reuse ladder: live tree → [proxy disk
  restore] → RAM cache (auto) → full prefill.
- `POST /chat/completions/input_tokens` → tokenizes a chat body with the server's own
  template + tokenizer, **no model compute**. Used as the conversation fingerprint.
- `GET /slots` → slot monitoring (idle state, token counts).
- KV reuse after restore is **pure token prefix matching** (LCP + checkpoints); edited
  histories degrade gracefully.
- **Queue semantics (verified)**: when the slot is busy, incoming tasks are
  *deferred* (`"no slot is available, defer task"`, `server-context.cpp:2847`);
  FIFO-with-priority, **no preemption**, and waiting is **KV-neutral** (the queue
  touches nothing). Multi-turn tool calls = sequential requests; the idle slot
  keeps its KV between turns → next turn reuses it (why tool loops are cheap).
  ⇒ **restore is the only destructive op** — it must land on an empty slot
  (drain-before-switch, see Operations).

## Conversation key (zero client cooperation)

**Hybrid: prefer `X-Conversation-Id` when present; otherwise infer.**

- Per request: call `input_tokens` → token list `T`. (One call; serves both identity
  and the save-threshold count.)
- **Same-conversation check**: does `T` extend a known conversation's last token tail
  (longest-tail prefix match)? → same conv, update its tail.
- **Else new conversation**: assign a **content-derived id** (e.g. hash of the root
  exchange) — deterministic, so filenames survive a *proxy* restart.
- **Forks**: two conversations extending the same tail → branch-split into two entries
  when they first diverge (longest-tail match picks the more specific tail).
- Header and inferred ids coexist in the same filename namespace; header wins when present.

Why tokens, not messages: the proxy's "same conversation?" decision then mirrors the
server's LCP decision by construction — same input, same rule, they can't disagree.
A client update that changes tokens (system prompt/template) breaks KV reuse and the
file key *together* — the file would have been useless anyway (the server full-prefills
even with warm RAM in that case).

**Memory-bounded parse**: `input_tokens` returns the full list (a 100K-token conv =
multi-MB JSON). The proxy needs only the **head** (root exchange → id hash) and the
**tail** (match window) → stream-parse the array, drop the middle. Bounded memory per
request.

**Known limitations** (accepted):

- One conv = one endpoint **format**: `/chat/completions` and `/v1/messages`
  tokenize differently (different templates) → the same logical conversation via two
  formats = two convs. Correct behavior (like a system-prompt change), just documented.
- Content-derived id = hash → collisions negligible (128-bit).
- Declared `X-Conversation-Id` is client-controlled → **sanitize to
  `[A-Za-z0-9._-]`** before use as a filename (server also validates via
  `fs_validate_filename`; the proxy shouldn't rely on 400s for it).

## Transparency, endpoints & deployment

The proxy is a **transparent reverse proxy**: all traffic to llama-server flows through
it. **Responses are byte-identical**; request bodies pass through unchanged **except
chat requests, where the proxy injects `id_slot`** (slot pinning — always on) **and
optionally `n_cache_reuse`** (targeted mode). Both are server-accepted fields; the
client never sees them. Routes probed from the beellama.cpp fork
(`tools/server/server.cpp`):

| Endpoint(s) | Slot? | Proxy behavior |
|---|---|---|
| `POST /chat/completions`, `/v1/…` | chat | **Intercepted** — full flow |
| `POST /completion(s)`, `/v1/completions`, `/responses`, `/v1/messages`, `/infill`, `/v1/audio/transcriptions`, `/v1/chat/completions/control` | yes | pass-through + **desk dirty** |
| `POST /embedding(s)`, `/v1/embeddings`, `/rerank(ing)` | yes (verified: slot pool, `SERVER_TASK_TYPE_EMBEDDING`) | pass-through + desk dirty |
| `POST /slots/:id` (no action), `POST /lora-adapters` | yes | pass-through + desk dirty |
| `POST /models`, `/models/load\|unload\|reload`, `DELETE /models` | router child lifecycle | pass-through + desk dirty (unload/reload = desk empty) |
| `DELETE /v1/stream` | stream/slot release | pass-through + desk dirty |
| `GET /health`, `/metrics`, `/props`, `POST /props`, `GET /models`, `GET /slots`, `GET /lora-adapters`, `POST /tokenize`, `/detokenize`, `/apply-template`, `*input_tokens`/`count_tokens`, `POST /v1/streams/lookup`, `GET /v1/stream`, `/cors-proxy` | no | pure pass-through |
| `POST /slots/:id?action=save\|restore\|erase` | — | proxy-internal only |

- **Deployment rule**: the server port is reachable **only via the proxy** — if any
  client mutates a slot directly, the proxy's action log goes stale silently.
- **Desk dirty** = a slot's `ram_since_restore` is invalidated (∅) → next chat for any
  conv is a restore candidate (restore if file exists). Same safe pattern as
  server/proxy restart.

## Save/restore event table (complete)

**SAVE — exactly one trigger:**

Intercepted chat response **fully received** (stream closed / body complete), AND
guard ON, AND conv length ≥ `MIN_SAVE_TOKENS`, AND conv is in the slot table (the
proxy knows which slot to save: `/slots/{id}?action=save`).

One save per response, fixed filename, slot idle. Non-chat slot mutators never save
(no conv key to name a file for).

**RESTORE — exactly one trigger:**

Identified conv **∉ `ram_since_restore`** (not in the live tree — *stronger* than
"≠ desk conv"), before forwarding, AND guard ON, AND `{conv}.bin` exists. Requires a
drained slot (in_flight == 0) and saves the target slot's conv first. No eager restore (not at
startup, not speculative), never a restore for a conv already in the tree.

**DELETE (hygiene):** restore→400 · shifted-suffix detected · LRU over cap · stale
`.tmp` at startup.

**No-strain invariants:**

1. Event-driven only — zero background timers touch the slot (sole poll: `/health`,
   read-only, for restart detection).
2. ≤ 1 save per response, ≤ 1 restore per switch — idempotent; duplicates impossible.
3. Save at slot idle, restore at slot empty — server never disturbed mid-work;
   client never sees a control call.
4. Guard OFF = zero control calls — pure pass-through.
5. All control calls serialized — one in flight, no chat during a restore.

## Desk state: how the proxy knows what's in the slot

The proxy never queries llama.cpp for KV state — it **orchestrates every action that
touches the slot**, so it knows each slot's contents from its own action log.
Canonical state (per model):

- **conv → slot table**: which conv lives in which slot (the proxy allocates — see
  slot awareness below).
- **`ram_since_restore` per slot**: convs forwarded into that slot since its last
  restore (= what's in that slot's live tree).
- **`in_flight` per slot**: chats forwarded but not completed.

"Warm" = arriving conv ∈ that slot's `ram_since_restore`. No API call.

**Invalidation events:**

- Restore(C) into slot S → S's `ram_since_restore = {C}`.
- Router restart (detected via `/health` flip / dropped connection) → **all** slots'
  sets = ∅, all tables cleared.
- Child unload/reload → that model's sets = ∅.
- Proxy restart → all sets = ∅ (assume cold); first request per conv is a restore
  candidate. (Conv *notebook* — tails for fingerprinting — is rebuilt from traffic;
  files/ledger recovered from disk.)

**Restore is destructive — the exact mechanism.** Restore clears the **live tree**
(tier 1) only; the RAM prompt-cache (tier 2) survives it. So the proxy must never
restore mid-generation, and must not restore a conv that's *already in the tree*
(the tree holds multiple branches across A→B→A until context pressure evicts).

The proxy tracks **`ram_since_restore`** = set of convs forwarded since the last
restore (= what's in the live tree). Reset to `{C}` on restore(C); ∪`{C}` on each
forward; `∅` on server restart.

```
on chat for conv C:
  if C ∈ ram_since_restore:          # C's KV is already in the live tree
      forward C                       # server LCP-reuses — NO restore
  else:                              # C not in the tree
      drain: wait until in_flight == 0   # restore must land on an empty slot
      if slot_conv ≥ threshold: save slot_conv   # preserve before destroying
      if file(C) exists:
          restore(C)   # 200 → ram_since_restore = {C};  400 → delete file, cold
      else:
          forward C cold   # tree keeps old convs; server may rescue C from
                           # tier-2 RAM cache, else full prefill
      ram_since_restore ∪= {C};  in_flight++
on C response complete:  in_flight--;  if C ≥ threshold: save C
```

**Why `∉ tree`, not `≠ desk`** (concrete): A → B(cold, no file) → A. After B, the
tree holds *both*. `A ≠ desk=B` would wastefully restore A; `A ∈ ram_since_restore`
just forwards and the server reuses A for free.

**Tier-2 (RAM-saved) interaction**: disk restore doesn't clear it — they coexist.
Tier 2 is the server's free *within-run* fallback (dies on restart); tier 3 (disk)
**survives restart**. Rule: **have a file → disk restore** (deterministic); **no
file → forward, let the server try tier 2**, else prefill. Residual cost: if tier 2
already had C, our disk read "wastes" a RAM-speed restore — unvisible to the proxy,
and the `∈ tree` rule eliminates the common case.

**Tracking is self-derived, one-sidedly safe.** `ram_since_restore` comes only from
the proxy's action log (never a server query). Entries leave only on restore/restart
(both true clears) → **never a wasted restore**. But the server can *evict* branches
under context pressure without telling the proxy → proxy may believe `C ∈ tree` when
evicted → skips restore → one full prefill. Cost: **≤ 1 missed warm start per
eviction**, self-heals on the next real switch. Conservative-by-miss, never-by-waste.
(Tier 1 = VRAM slot KV only; tier 2 is separate CPU-RAM, opaque to this tracking.)

**Lifecycle**: the proxy's "server" **is the router process** (`--models-preset`
top-level); proxy and router share a lifecycle (unit/compose). Health-poll targets
the router. **Router restart → all children's trees AND RAM tiers die** → clear
every model's desk. Child `load`/`unload` at runtime → per-model desk invalidation
only (desk-dirty rule). Proxy+router dying together is fine — **disk is the
persistence layer**; content-derived filenames + `stat()`-rebuilt ledger recover it.

**Slot awareness: the proxy is the slot allocator.** Verified facts:

- Server's selection ladder (`server-context.cpp:1730+`): **pinned id →
  restorable-prefix match (if `slot_prompt_similarity` set) → LRU**. On displacing a
  slot with <50% keep, the server auto-saves the old KV to the RAM tier first.
- **Requests can pin a slot**: body field `"id_slot": N`
  (`server-context.cpp:5089`).
- **Responses report the slot used**: `res->id_slot` (`server-context.cpp:2448`).

Design: per-model **conv → slot table**; the proxy **injects `id_slot` into every
chat body** (same mechanism as `n_cache_reuse`) → it always knows exactly which slot
every conv lives in → save/restore target the right slot, zero guessing.

- **Allocation** (mirrors server LRU): new conv, all slots held → pick LRU conv's
  slot → save it → restore new conv into it. Drain rules unchanged.
- **Slot count**: read at startup (`GET /slots` / config) per model. **Single slot =
  the degenerate one-row table** (current deployment) — the "desk" *is* the allocator
  with N=1; no special case.
- Pinning makes the proxy *authoritative* (server does what it says) instead of
  reactive (chasing choices via `res->id_slot`) — consistent with "the proxy
  orchestrates every slot action."

## Per-request flow

1. Receive chat request.
2. `input_tokens` → token list `T` (length `L`).
3. Identify conversation:
   - Header present → use declared id.
   - Else longest-tail match → same conv (update tail) **or** new conv:
     - **Shifted-suffix check** (new-conv path only): does `T`'s tail match a known
       conv's tail with overlap ≥ `TAIL_MATCH_MIN` (e.g. 64 tokens)? → that conv was
       rewritten (compaction / head change / middle edit) → **delete its file now**
       (provably dead). No match → genuinely new (or a full-rewrite — undetectable,
       LRU handles it).
4. **Restore check** (see "Desk state" for the full mechanism): `conv ∉
   ram_since_restore` (of its allocated slot)?
   - No → conv is in the live tree → forward directly (server reuses; no restore).
   - Yes → **allocate a slot** (conv's own, or LRU conv's slot if all held — save the
     displaced conv first) → drain in-flight → if `{conv}.bin` exists: `restore` into
     that slot before forwarding (200 → `ram_since_restore = {conv}`; 400/fail →
     delete file, continue cold). No file → forward cold.
5. Forward the chat request **with `id_slot` injected** (the allocated slot); stream
   the response back to the client.
6. **On response completion (turn boundary)**:
   - **Save decision**: `L ≥ MIN_SAVE_TOKENS`? (use `T`'s length — already in hand; the
     previous save's `n_saved` is a better size estimate and feeds the *next* decision.)
     - Yes → `save` to `{conv}.tmp` → on 200 rename → `{conv}.bin`; ledger += `n_written`.
     - No → skip (small conv, prefill cheaper than the file I/O).
   - Update conv `last_seen`.
7. **Eviction**: files > `N_MAX` (3–5) or total > `MAX_BYTES` → evict
   **oldest-used** (LRU by `last_seen`, never a conv currently in a slot). "Oldest
   created" is meaningless — the file is rewritten every save. Sizes from
   `n_written`/`n_saved` — exact.
8. **Thrashing guard**: rolling window of `K` requests; > 1 conv-switch per window →
   **pause save AND restore** (a file never restored is pure overhead; they're one
   unit). `last_seen` still updates while paused. Hysteresis: back ON only when the
   whole window has ≤ 1 switch. No race with the save decision — guard, save, and
   eviction evaluate in the same event loop, in order; saves are idempotent, so a
   skipped save is a slightly older file, never lost data. On re-engage, the first
   restore uses the pre-pause file (valid snapshot; next request re-prefills from
   that point). Works on the hybrid key — no header required.

## Save / restore timing (summary)

| Action | When | Why |
|---|---|---|
| save | after response fully completes | slot state == conversation state; server defers until idle anyway |
| skip save | `L < MIN_SAVE_TOKENS` | small convs: prefill < file cost |
| restore | conv **∉ live tree**, before forwarding | one restore per switch, never per request; no eager restore at startup |
| delete file | restore 400 / shifted-suffix detected / LRU evict | dead files don't accumulate |
| serialize | no chat forwarded during a restore | restore clears the slot; racing a chat into it = wasted restore |

## Compaction, head changes & history rewrites (shifted-suffix family)

Every case here is the **same shape**: a *suffix* of the prompt is unchanged, but
everything before it shifted. The tail-matcher detects them all (new conv whose tail
matches an old tail → old file deleted + targeted `n_cache_reuse` injected); the server
slides the unchanged suffix's KV to its new offset. Instances:

- **Compaction** (middle shrink): `[system][long-history]` → `[system][summary]`.
- **Head change** (system prompt / cwd / tools / template): `[system_old][conv]` →
  `[system_new][conv]` — e.g. **moving a chat to a new directory** (cwd in the system
  prompt). Needs the conversation longer than the tail window, else the tail includes
  the system prompt and the match fails → genuinely new conv.
- **Middle edit**: `[system][A][B]` → `[system][A'][B]` (B = tail, unchanged).

All = **new conversation at both layers** (server KV: LCP ≈ 0 → full prefill; proxy:
new key → new file). Without `n_cache_reuse`, the one full prefill is unavoidable — the
server pays it with warm RAM too. With it (targeted), only the changed prefix
re-prefills; the unchanged suffix's KV is slid.

- **Detection threshold**: tail overlap ≥ `TAIL_MATCH_MIN` (64). Below → no detection →
  LRU is the safety net. False positive cost = one future cold start (cheap).
- **Middle-edit** (non-compaction history rewrite): looks like a new conv; tail matches
  old → old file deleted. Accepted loss: post-restart partial-prefix reuse of the old
  file (≈ one short prefill).
- **`n_cache_reuse` (optional, default OFF)**: server-side, live-RAM only. When a
  shifted-suffix rewrite happens (compaction / head change / middle edit), the
  unchanged suffix shifts position;
  `n_cache_reuse` slides already-computed KV chunks to their new offset (`seq_add(...,
  kv_shift)`) instead of re-prefilling. ELI5: "I already read page 50; the book lost 10
  pages from the front, so move my bookmark — don't re-read." Orthogonal to disk
  save/restore (the file is always a full snapshot). Disabled for multimodal; needs
  shift-capable memory.
  - **It's a server param with two levels**: server default (`params_base`) and a
    **per-request body field** (verified in schema). The proxy can set it **at runtime**
    by injecting `"n_cache_reuse": N` into the forwarded chat body — no server restart.
  - **Targeted mode (preferred)**: the proxy is the only thing that *detects* a
    shifted-suffix rewrite, so it injects `n_cache_reuse` **only on that one request**
    (new conv whose tail matched an old tail) — exactly when a shifted suffix exists
    in the live tree. Off otherwise → zero cost when unneeded. Trade-off: the proxy writes that
    one chat body (adds a field) — no longer byte-identical on it; harmless.
  - **Simple mode (v1)**: set a server-level default, proxy doesn't touch it.

## Operations & edge cases (audit)

1. **Drain before switch**: a chat for conv X still streaming when conv Y arrives →
   hold Y until X's response finishes → save X → restore Y → forward Y. Restore is the
   one operation that *destroys* slot state; it must land on an empty slot.
2. **Client disconnect mid-stream** = turn boundary. Slot holds prompt + partial
   generation — a valid prefix state → save it (idempotent). "Stream ended for any
   reason" triggers the save, not just clean completion.
3. **Save failure** (disk full / 500): log + skip this turn; retry at the next turn
   boundary (idempotent, latest state wins). The client's chat never depends on the shelf.
4. **API key**: if the server runs with `--api-key`, the proxy's control calls
   (`input_tokens`/`save`/`restore`) must carry it; client `Authorization` passes through.
5. **Shared filesystem**: the server *writes* files (`--slot-save-path`); the proxy
   *deletes* them directly (no delete API) → both must see the same path (same host,
   or shared Docker volume).
6. **Observability**: structured logs only — save (conv, `n_saved`, ms), restore
   (hit/400), evict, guard on/off, shifted-suffix detected. No UI/state endpoint.
7. **Slot persistence (verified)**: this fork has **no slot idle-release timer** — the
   desk stays in RAM until context pressure. `--cache-idle-slots` (RAM tier) is
   server-internal and transparent. The desk cannot silently empty itself.
8. **Control-call failures** (the proxy's own calls, not the client's chat):
   - `save`/`restore` **timeout** (knob `control_timeout_ms`, default ~10 s) → treat
     as failure: skip save / delete file + continue cold.
   - **Connection drop** → fail fast with 502 (no TCP-timeout waiting) + immediately
     mark **all** desks empty (same as router restart).
9. **`input_tokens` failure** (fingerprint is on every request's critical path):
   retry once → then **degraded pass-through** for that request (forward without
   control ops, mark desk dirty). Fingerprinting never takes chat down.
10. **Latency budget** (the design's cost profile, explicit):
    - Every chat: +1 `input_tokens` round-trip (local, ms-scale; no model compute).
    - Per conv switch: +restore (file read + validate; tens–hundreds of ms for large
      files). Once per switch, client-visible, buys the warm start.
    - Per turn: +save (at the turn boundary, slot idle).

## Miss debugger (niche-case observability)

The design handles the common cases by construction; niche misses (e.g. **moving a chat
to a new directory** → cwd in system prompt → head tokens change → full pp, *correctly*
a new conversation) need a post-hoc causal chain to diagnose and decide on.

**Per-request correlation record** (DEBUG level, one structured line per request):

- `request_id` (proxy-assigned; maps to server `cmpl_id`)
- **Proxy verdict**: same-conv (id) | new-conv (closest candidate id)
  - if new: **first-divergence index** vs closest candidate + **detokenized window**
    around it (via `/detokenize`) — this is the "byte diff that caused the miss"
- **Server verdict** (from the response itself): `n_prompt_tokens_cache`,
  `timings.cache_source`, `timings.cache_reason`, `timings.cache_lcp_n`
- **Proxy actions taken**: save / restore / skip / evict (with sizes)

**Miss taxonomy** — every full prefill is classified into exactly one bucket, so logs
are aggregable and decisions are referenceable:

1. **Genuinely new conv** (no tail/head relationship to any known conv)
2. **Head change** (system prompt / tools / cwd / template) — divergence at early index
3. **Middle edit** (tail matched, middle diverged)
4. **Compaction** (tail matched, new-conv path)
5. **Server-side loss** (restart / eviction / restore-400) — proxy knows its own actions
6. **Tier-2 miss** (server `cache_reason`)

**Debug artifacts**: each new-conv event writes a JSON artifact
`{request_id, ts, closest_candidate, divergence_index, detokenized_window,
head_hash, tail_hash}` to a debug dir (rotated, bounded). This is the reference for
evolve-the-proxy decisions ("oh, 40% of my 'new convs' are cwd head-changes →
normalize cwd out of the fingerprint, or accept it").

**Log levels**: INFO = decisions (save/restore/evict/guard); DEBUG = fingerprint +
diff + server reuse report; TRACE = full token lists.

## Failure modes (all degrade to cold start)

| Failure | Handling |
|---|---|
| Truncated save (crash mid-write) | `.tmp` + rename; delete stale `.tmp` at startup |
| Bad/old file on restore | 400 → delete file, continue cold |
| Restarted with smaller `--ctx-size` | restore 400 → delete file, continue cold |
| Router restart | **all** models' desks clear; files persist; lazy restore on first switch |
| Child model unload/reload | that model's desk clears; files persist; lazy restore |
| Proxy restart | content-derived ids → same filenames; notebook rebuilt from first requests |
| Thrashing traffic | adaptive pause of save/restore |
| Tail too small to fingerprint | no shifted-suffix detection; LRU evicts |
| `save`/`restore` timeout | skip save / delete file + continue cold (`control_timeout_ms`) |
| `input_tokens` failure | retry once → degraded pass-through + desk dirty (chat never blocked) |
| Connection drop to server | fail fast 502 + all desks empty (recovery = lazy restore) |

## Configuration

**Single centralized JSON config** (`config.json`), self-documenting like a CLI tool.
Flags: `--config <path>` (location; default `<project root>/config.json`),
`--generate-config` (emit every key with default + accepted values + description),
`--version`. Precedence: **config file > built-in default** — no per-key CLI flags;
the file is the one surface. Auto-generated with defaults if missing.
`--generate-config` output (mirrors skillsync): a `# <key>: <description>` line per
key, then the full JSON with defaults.

**Always-on behaviors (not knobs)**: `id_slot` pinning on every chat (slot
allocator); slot count read at startup per model (`GET /slots`); control calls
serialized.

**Knobs** (all tunable; safe defaults shown; "Accepted" = the valid domain):

| Knob | Type | Default | Accepted | Meaning |
|---|---|---|---|---|
| `listen` | string | `127.0.0.1:8080` | `host:port` | proxy bind address |
| `upstream` | string | `127.0.0.1:5000` | `host:port` | llama-server (router) address |
| `save_path` | string | (server's `--slot-save-path`) | existing dir | where `.bin` files live (must match the server's) |
| `min_save_tokens` | int | `512` | `≥ 0` | skip saving below this length |
| `n_max_files` | int | `4` | `≥ 0` (`0` = ∞) | max files on shelf |
| `max_bytes` | int | `2147483648` (2 GiB) | `≥ 0` (`0` = ∞) | max total shelf bytes |
| `thrash_window` | int | `8` | `≥ 2` | rolling requests for the guard (`K`) |
| `thrash_max_switches` | int | `1` | `≥ 0` | switches allowed per window before pause |
| `tail_match_min` | int | `64` | `≥ 1` | min tail overlap for shifted-suffix detection |
| `cache_reuse` | int | `0` | `≥ 0` (`0` = off) | `n_cache_reuse` min chunk size to inject on a shifted-suffix |
| `cache_reuse_mode` | enum | `targeted` | `targeted` \| `always` | when to inject `n_cache_reuse` |
| `health_poll_ms` | int | `3000` | `> 0` | `/health` poll interval (router-restart detection) |
| `control_timeout_ms` | int | `10000` | `> 0` | timeout for proxy-issued `save`/`restore`/`input_tokens` calls |
| `api_key` | string | `""` | any string | server's `--api-key`, carried on control calls |

### Threshold estimate (`min_save_tokens`)

Break-even: re-prefill `L/PP_speed` vs save+restore `(KV_bytes/token × L)/disk_speed + overhead`.
`KV_bytes/token = layers × 2 × kv_heads × head_dim × elem_bytes` (~128 KB/token, 8B-class f16).
At **600 t/s** on SSD, disk cost ≪ prefill for any practical `L` → threshold is a
*"will it come back?"* heuristic, not a cost break-even. **Default 512**; LRU bounds the
shelf. Re-derive if model/disk changes.

## Test strategy

**Unit** (no server): tail matcher (extend/fork/shifted-suffix), LRU eviction +
hysteresis guard, filename sanitization, ledger math (max_bytes with many small
files), tmp/rename logic.

**Integration** (real `llama-server`, pinned commit):

1. **Round-trip**: save → kill server → restart → restore → verify warm (no full
   prefill; check timing metrics).
2. **A→B→A no-waste**: verify **no** restore fires for A on the return (A ∈ tree).
3. **Shifted-suffix delete**: compacted conv **and** head-changed conv (cwd move) →
   old file deleted, new conv saved, and (targeted) `n_cache_reuse` injected.
4. **Thrashing**: A/B/A/B → guard pauses → verify zero control calls while paused.
5. **Restore 400**: corrupt a file → restore 400 → file deleted → cold continue.
6. **Proxy restart**: kill proxy mid-life → restart → ledger rebuilt via `stat()`,
   files still found (content-derived ids).
7. **Drain**: start conv Y's request mid-X-stream → Y held until X completes.
8. **Idempotency**: double-save same conv → identical file, no state corruption.

**Property**: saves idempotent; eviction never removes an in-slot conv; guard ON/OFF
flaps bounded by hysteresis.

## Resolved decisions

- **Multi-model**: per-model desk + `{model}/{conv}.bin`; restore targets the child slot
  via router `model` field. (Folded into design, not open.)
- **`n_cache_reuse`**: optional flag, default OFF (see shifted-suffix section).
- **Fork-split** edge cases: longest-tail match handles fork-of-fork; fork-after-compaction
  is a new conv (tail check runs on the new-conv path). Accepted as-is.
