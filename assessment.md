# Proxycache — Assessment

Full-repo code assessment. All 7 source files read in full; outbound API
interfaces verified against the live llama.cpp server at
`yusrils-desktop.local:30001` (read-only + one slot save probe) and against
`ggml-org/llama.cpp` `tools/server/README.md` (master).
See `flows.md` for the branch-by-branch code walk.

---

## 5W1H

| Question | Answer |
|---|---|
| **What** | A FastAPI/uvicorn reverse proxy that sits in front of one or more llama.cpp `llama-server` instances and implements the OpenAI-compatible `POST /v1/chat/completions` + `GET /v1/models` endpoints. Its job: manage llama.cpp *slots* (each slot = one conversation's KV cache) so repeated/similar prompts skip full re-prefill. |
| **Why** | llama.cpp KV caches live in RAM per slot. Re-prefilling a 30–60k-token IDE prompt takes minutes; restoring a saved cache from disk takes seconds. With few slots and many users, naive routing overwrites hot caches. The proxy adds: LRU slot allocation, prefix-similarity (LCP over SHA-256 word-blocks) matching, save-to-disk / restore-from-disk of evicted caches, per-model meta indexing. |
| **Who** | Written by the `airnsk` GitHub account (repo `airnsk/proxycache`, RU/EN READMEs, Russian log strings/comments — likely a Russian-speaking author). Target users: dev teams sharing one llama-server (e.g. 20 devs, 4 slots), IDE-assistant workflows (Cline + gpt-oss-20b mentioned in README). |
| **When** | Single process running next to llama.cpp, port 8081 by default (config `PORT`). State: in-memory slot table + on-disk `.meta.json` files in `kv_meta/` (CWD-relative). |
| **Where** | Proxy: any host (default bind `0.0.0.0:8081`). Backend: llama.cpp server(s) configured via `BACKENDS` env JSON (`url` + `n_slots` per backend) — supports multiple llama.cpp instances/models (added in PR #3 "multi_llamas"). |
| **How** | 1. Hash request prefix → word-blocks → SHA-256 block hashes. 2. If "big" (>500 words), scan meta dir for best LCP-ratio candidate ≥ 0.6 → restore key. 3. Acquire a slot: free first, else oldest LRU, under per-slot asyncio lock (300 s timeout). 4. If restore key: `POST /slots/{id}?action=restore` into that slot. 5. Forward chat request with slot pinned (root body + `options` body + query params). 6. On completion: `POST /slots/{id}?action=save` (big requests only), write meta file, release slot. Streaming path runs a background reader task that pushes chunks into an `asyncio.Queue` and owns save/meta/release in its `finally`. |

## Architecture in one paragraph

`proxycache.py` (entry) → `app.py` (FastAPI, request orchestration) →
`slot_manager.py` (slot table, free/oldest LRU, per-slot `asyncio.Lock`) →
`llama_client.py` (httpx async client per backend: chat, save, restore, models)
→ `hashing.py` (prefix/block hashing, meta scan/lookup/write) → `config.py`
(env-driven constants). ~700 LOC total, no tests, no Docker, single venv.

## Verified outbound API surface (llama.cpp)

Verified against the live server **in router mode** (no `-m` at router level,
model `27B-Q3.8` loaded on internal port 36899, exposed via the router on 30001):

| Proxy call | llama.cpp endpoint | Status on live server |
|---|---|---|
| `GET /v1/models` (`get_model_id`) | OpenAI models list | ✅ works; router forwards from loaded instance. Returned `id: "27B-Q3.8"` |
| `POST /v1/chat/completions` w/ `model` in body | OAI chat | ✅ router routes by body `model` (proxy always sets it → OK) |
| `POST /slots/{id}?action=save` body `{"filename": ...}` | slot save | ⚠️ **400 "model name is missing"** from router. Adding `?model=` query param **does not help**; model must be **in the JSON body**: `{"filename":..., "model":"27B-Q3.8"}` → success (`n_saved: 76321`, 1.76 GB written in 1.2 s) |
| `POST /slots/{id}?action=restore` body `{"filename": ...}` | slot restore | ❌ same router requirement → proxy will 400 (not probed to avoid clobbering the live slot) |
| slot pin via root `id_slot`/`slot_id`/`_slot_id` + `options` + query | `/completion` documents `id_slot` in body; chat endpoint supports `/completion`-specific features | ✅ `id_slot` at root is documented; the extra `_slot_id`/`options`/query pinning is defensive legacy (older llama.cpp accepted `options` body), harmless |
| `cache_prompt`, `n_keep=-1` in body | documented `/completion` options | ✅ supported; note the live server already runs `--keep -1` |

**Live server facts (read-only probes):** 1 slot (id 0, n_ctx 135168,
`--slot-save-path` set, `--cache-idle-slots --cache-prompt --jinja`,
custom Qwen jinja template, speculative draft flags present),
`GET /slots` **requires** `?model=` in router mode.

## Findings & recommendations

### Bugs / compatibility (high priority)

1. **Router-mode save/restore is broken** — the verified deployment is router
   mode, and `llama_client.save_slot/restore_slot` send only `{"filename": ...}`.
   Router demands `model` in the JSON body (query param is ignored). Fix:
   include `model` in the save/restore body (the client knows it via
   `get_model_id()` / `BACKENDS` config). This means **the proxy's entire
   save/restore value proposition is silently disabled today**: every
   `save_after` returns `False` (400 → `raise_for_status` caught? no —
   `save_slot` only special-cases 500; a 400 raises `HTTPStatusError`
   uncaught → in the non-stream path it becomes a 500 to the client even
   though the chat itself succeeded; in the stream path it's swallowed by
   the reader's try/except and only logged).
2. **400 from `save_slot` is unhandled in non-stream path** — `sm.save_after`
   raises through `chat()`'s generic `except` → 500 returned *after* the
   completion already ran. Even if save fails, the answer should be returned.
3. **`n_slots` must be set manually per backend and matches nothing** —
   the server here has 1 slot; proxy default `N_SLOTS=2` would address a
   non-existent slot 1. `GET /slots` (or `/props` `total_slots`) exists —
   the proxy could discover this at startup instead of trusting env.
4. **`_is_free` conflates "free" with "never used"** — a slot is "free"
   only if `_last_used == 0.0`. After the first save, *no slot is ever free
   again*; everything becomes "oldest LRU". Combined with #1 (save failing),
   `save_after` never updates `_last_used` on failure, so failed saves leave
   slots permanently "free" — the two bugs interact subtly.
5. **Meta files are never GC'd** — `touch_meta` exists but is **dead code**
   (never called anywhere). Meta files accumulate forever in `kv_meta/`;
   `scan_all_meta()` reads *all* of them on every big request (O(all metas)
   file I/O per request, and it's sync — it blocks the event loop).
6. **`scan_all_meta`/`find_best_restore_candidate` run on the event loop
   thread** — dozens of meta files × JSON parse per request will stall
   concurrent streaming. Move to `asyncio.to_thread` or index in memory.
7. **Restore correctness gap** — after `restore_slot` into slot g, the
   request is sent with `n_keep=-1, cache_prompt=true`; llama.cpp reuses KV
   only if the *prompt prefix* matches what was restored. LCP matching is
   done on word-block hashes of *content only* (roles stripped, `"\n\n"`
   join) — a cheap proxy for "similar", but the key includes `model_id +
   "\n" + raw_prefix` while the restore candidate search compares *blocks*,
   so two requests with identical blocks but different trailing content get
   different keys yet can restore each other's cache — fine for reuse, but
   then `save_after(g, key)` writes a **new** meta file whose block list
   doesn't correspond to what's actually in the slot's cache (the slot
   now holds the new request's KV, so that's OK) — the real hazard is
   **restore into a slot that already holds a *different* live conversation
   the proxy thinks it owns**: the proxy's LRU "oldest" eviction never saves
   the evicted slot first (README describes "save oldest before using it",
   code in `slot_manager.acquire_for_request` just picks free/oldest and
   optionally restores — **no pre-eviction save**). The README's "save before
   evicting" behavior is not implemented.
8. **Streaming: client disconnect does not cancel the reader task** —
   `gen()` is consumed by StreamingResponse; if the client disconnects,
   `gen` is closed, but the `reader` task keeps reading llama.cpp to the end
   (queue fills, `put` blocks, reader stalls on a full queue because
   `get` stopped → reader hangs forever at `queue.put`, holding the slot
   lock indefinitely). There is no `aclose()`/finalization hook wired to the
   reader task, and no timeout on the blocking `put`.
9. **`ACQUIRE_TIMEOUT` cancels `acquire_for_request` mid-`lock.acquire()`** —
   `asyncio.wait_for` cancels the task; the lock may be acquired *inside*
   the task and never released (a cancelled `await lock.acquire()` that
   just got the lock leaks it), and the restore may have already been issued.
   Use `async with`-style guarded acquisition.
10. **`get_model_id()` per request, from `clients[0]` only** — every request
    pays an extra HTTP round-trip to backend 0, and multi-backend setups
    key metas by backend-0's model id even for requests served by backend
    1 (`backend_model_id` is used for the key and meta, not the serving
    client's model id).
11. **`/v1/models` always reports `MODEL_ID`** — fine for OAI clients, but
    in multi-backend mode the proxy can serve different models under one
    name.
12. **Minor** — `@app.on_event` is deprecated in modern FastAPI (use
    lifespan); `httpx` 0.28 `resp.raise_for_status()` on a failed restore
    inside `acquire_for_request` propagates to the 500 path *after* the
    lock is held → released by outer `except`, OK, but the 300 s
    `ACQUIRE_TIMEOUT` wraps restore too, so a slow restore competes with
    the acquire budget; `STREAM_QUEUE_SIZE=16` unbounded-bytes (chunk size
    unbounded); `touch_meta` dead; `hashing.words_from_text` lowercases +
    `\w+` (unicode), so word count ≠ token count (threshold is words,
    fine, but document it); meta `write_meta` in non-stream path is not
    wrapped in try — a disk error 500s a successful completion.

### Technical improvements

- **Make the slot table match reality**: query `GET /slots?model=...` at
  startup (works, verified) to learn `n_slots` and busy state; reconcile
  periodically. llama.cpp's own `is_processing` + `n_prompt_tokens` are
  better LRU signals than the proxy's guess.
- **Use llama.cpp features that subsume this proxy** (master README,
  verified): `--slot-prompt-similarity` (server-side slot reuse by prompt
  similarity, default 0.10), `--cache-ram` (RAM-backed KV that survives
  slot eviction, default 8 GiB), `--cache-idle-slots` (auto save/restore of
  idle slots — already on the live server!), `-ctxcp/--ctx-checkpoints`
  (context checkpoints). The live server runs `--cache-idle-slots` —
  llama.cpp is already doing idle-slot save/restore natively; the proxy's
  added value shrinks to *cross-request LCP targeting and multi-backend
  fan-out*. Worth re-evaluating the proxy's scope.
- **Replace per-request meta glob-scan** with an in-memory
  `{key: blocks}` index loaded at startup, updated on write, with
  size-bounded LRU eviction of meta+cache pairs.
- **Pre-eviction save**: implement the README's "save oldest before using"
  (one `save_slot` call in `acquire_for_request` when evicting a slot
  with a known key) — small change, big correctness win.
- **Structured observability**: the log lines are good (`before_acquire`,
  `dispatch`, `json_done`); add a `/metrics` (prometheus) endpoint —
  hit/restore/save rates, queue depth, slot table state.
- **Testing**: zero tests. `hashing.py` is pure functions — trivially
  unit-testable; `SlotManager` is testable with fake clients; the
  stream-reader/queue lifecycle is the riskiest code and has no coverage.

### Vision / product side

- The README sells "hot/cold" semantics the code no longer has (simplified
  away in `slot_manager` — README and `readme_RU.md` describe the old
  hot/cold design, code is free/oldest-only). Docs drift is the biggest
  current risk; the RU README even references env vars
  (`LLAMA_SERVER_URL`, `SLOTS_COUNT`, `SIMILARITY_MIN_RATIO`,
  `MIN_PREFIX_*`) that no longer exist in `config.py`.
- Positioning: with llama.cpp adding native idle-slot caching and
  prompt-similarity slot selection, this proxy's defensible core is
  (a) multi-backend/multi-model fan-out with per-model cache keys, and
  (b) team-level KV persistence across llama.cpp restarts. Lean into
  that; consider also acting as the *single stable base_url* for IDEs
  (the actual user pain per the Cline link), which is arguably the
  feature users remember it for.
- Missing for production: auth (it's a 0.0.0.0 port), per-user
  isolation/accounting, cache file size accounting (a 27B Q3.8 slot is
  1.76 GB per save — the probe wrote one), graceful handling of backend
  restarts (slots vanish; proxy table doesn't know).

### One-line verdict

A correct-in-spirit, small and readable KV-cache-aware proxy, currently
**silently degraded** against its own verified deployment (router-mode
save/restore 400s, meta scan on the event loop, no pre-eviction save,
stream-reader leak on client disconnect). Five focused fixes (router `model`
in save/restore body, non-fatal save errors, meta index + GC, reader
finalization, slot discovery) would make the README's design true.
