# Proxycache — Upgrade Plan

Ordered by priority. P0 is a one-line-class fix that unblocks everything;
P1 is the architectural shift ("thin layer over the router"); P2+ harden the
value-add logic. References point at current `main` (c0f7983).

Verified facts used below (see `assessment.md`):
- Live deployment is llama.cpp **router mode**; `POST /slots/{id}?action=save|restore`
  requires `model` in the JSON **body** (query param ignored).
- `GET /slots?model=...` and `GET /v1/models` work through the router.
- Chat completions route by `model` in body — the proxy already preserves it.

---

## P0 — Router-mode save/restore (do first, independently of everything else)

**Why:** The proxy's entire value proposition is currently disabled against
the production server: `save_slot`/`restore_slot` 400. In the non-stream path
this even turns successful completions into 500s.

**Change:** `llama_client.py`
- `LlamaClient` carries a `model` name (from config or a startup
  `get_model_id()` — see P1, which makes this per-request from the body).
- `save_slot` / `restore_slot`: send `{"filename": ..., "model": ...}`.
- Make 400/4xx from save *non-fatal*: `save_slot` returns `False` on any
  non-2xx (mirror `restore_slot`), never raises.

**Also here (cheap, same files):**
- `app.py` non-stream path: wrap `save_after` + `write_meta` in the existing
  `try` so a save/meta failure logs and returns the completed answer with 200.
- Remove the dead `502 non-JSON` branch (`not isinstance(out, dict)` is
  unreachable — `chat_completions` always returns a dict; the two provider
  error dicts should instead map to 502 explicitly).

**Acceptance:** against the router, a big request logs
`save_after ok=True`; killing the llama.cpp save dir returns 200 for the
chat with `saved=False` in logs.

---

## P1 — Transparent pass-through architecture

**Why:** minimize maintain surface (agreed direction). The proxy becomes a
thin layer over a router/single server; everything it doesn't understand is
forwarded byte-for-byte, including future llama.cpp endpoints.

**Changes:**
1. `app.py`: replace explicit routes with a catch-all
   `@app.api_route("/{path:path}", methods=["GET","POST","DELETE","PUT","PATCH","OPTIONS"])`
   that pipes: method, full path + query, headers (minus `host`, keep
   `authorization`), body (raw bytes, not re-serialized JSON) → router;
   response: status, headers (minus hop-by-hops), streamed body
   (`client.stream` + `aiter_raw` → `StreamingResponse`).
2. Two interceptors registered *above* the catch-all:
   - `POST /v1/chat/completions` → the existing watch logic (below).
   - `POST /slots/{id}` with `action=save|restore` → inject `model` into
     JSON body, then pass through.
3. **Stop rewriting model identity:**
   - `GET /v1/models` passes through (drop the static `MODEL_ID` route).
   - The key/model id comes from the request body's `model` field (already
     read as `client_model`); drop the per-request `get_model_id()` call.
   - `MODEL_ID` env remains only as a fallback for clients that send none.
4. `llama_client.py`: add a raw pass-through method
   (`stream(method, path, headers, content)`); existing typed methods stay
   only where the interceptors need parsing.

**What disappears:** `models()` route, `MODEL_ID` coupling, and the need to
ever touch the proxy for new llama.cpp endpoints (`/v1/responses`,
`/v1/messages`, `/tools`, UI, `/metrics`, …).

**Acceptance:** `curl` the proxy and the router directly for
`/health`, `/props?model=`, `/slots?model=`, `/v1/models`, Web UI —
byte-identical responses.

---

## P2 — Slot table discovery (kill `N_SLOTS` env)

**Why:** env `n_slots` matches nothing; the live server has 1 slot.
Router mode makes it per-model.

**Change:** `slot_manager.py`
- At startup (and on a periodic/background refresh, e.g. every 30 s):
  `GET /slots?model={model}` per known model → real slot count + live
  `is_processing` / `n_prompt_tokens` / `id_task`.
- Slot selection uses server state as the LRU signal when available
  (oldest by `n_prompt_tokens` activity / absence of `is_processing`),
  falling back to the internal `_last_used` when the endpoint is
  unreachable.
- `BACKENDS` config becomes `{"url": ...}` only; no `n_slots`.

**Acceptance:** change `-np` on the server, restart proxy → table matches
without env edits.

---

## P3 — Meta index + GC (stop globbing per request)

**Why:** `scan_all_meta()` does O(all files) sync I/O on the event loop for
every big request; `touch_meta` is dead; files never evict (1.76 GB KV per
big save — disk is the real constraint).

**Changes:**
- Load `{key: meta}` into memory at startup from `META_DIR`; update on every
  `write_meta`. Lookup = in-memory LCP scan (already linear over metas, now
  no I/O); move the scan to `asyncio.to_thread` if the set grows large.
- Eviction: size-bounded LRU over meta entries — on evict, delete
  `{key}.meta.json` *and* ask llama.cpp to erase (optional: the KV file
  lives under `--slot-save-path`; deleting the `.bin` by name is safe since
  names are the keys). Cap total KV bytes via env.
- Delete `touch_meta` (dead) or wire it to "meta touched on hit" for the
  LRU.

**Acceptance:** 10k meta files → big-request latency unchanged; disk usage
stays under cap.

---

## P4 — Pre-eviction save (make the README true)

**Why:** README promises "save oldest slot before using it"; code just
picks free/oldest and overwrites. Today the only saves are post-request,
so a slot's cache is lost whenever it's evicted *without* its owner
requesting a save (small requests, failed saves, restart).

**Change:** `slot_manager.acquire_for_request`
- When the chosen slot is "occupied" (has a known key, not the restore
  target), issue `save_slot(slot, known_key)` before handing it out
  (fire-and-wait with a short timeout; failure → drop and continue).
- Requires tracking `slot → key` (already implicitly known: the last
  request that used the slot) — add `self._slot_keys: Dict[GSlot, str]`.

**Acceptance:** two big conversations A, B with slots=1 → after B, A's
`.bin` exists on disk and A's next request restores (measured: prompt
processing skips the full prefix).

---

## P5 — Stream reader lifecycle (fix the slot-lock leak)

**Why:** client disconnect leaves the reader blocked on `queue.put` with a
full queue, holding the slot lock forever; the reader task is
fire-and-forget with no finalization hook.

**Changes:** `app.start_stream_task`
- Wire finalization: `asyncio.shield`-free pattern —
  `asyncio.create_task(reader)` + `task.add_done_callback` that logs;
  in `gen()`'s `finally` (called when StreamingResponse closes the
  generator, including on client disconnect): if reader still running →
  `await reader_task` after cancelling the *send* side is not possible on a
  raw httpx stream, so instead: `queue.put` with a timeout in a wrapper
  coroutine that, on timeout, closes `resp` (which unblocks `aiter_raw`)
  and awaits the reader to run its `finally` (save/meta/release).
- Simpler equivalent: make the reader tolerate a dead consumer —
  `put` via `asyncio.wait_for(queue.put(chunk), 5.0)`; on timeout close
  `resp`, drain `finally`.
- Never block unbounded in `put`.

**Acceptance:** `curl --max-time 1` mid-stream → slot released within ≤5 s
(`GET /slots` shows `is_processing` false / proxy log `stream_reader_done`).

---

## P6 — Acquire robustness

**Why:** `asyncio.wait_for` around `acquire_for_request` can cancel
mid-`lock.acquire()`/mid-restore and leak the lock (then that slot is
permanently 503/500).

**Changes:** `slot_manager` / `app.chat`
- Guarded acquisition:
  ```python
  async def acquire_for_request(...):
      g, lock = self._get_free_or_oldest()
      try:
          await asyncio.wait_for(lock.acquire(), timeout=ACQUIRE_TIMEOUT)
      except asyncio.TimeoutError:
          return None, None, None          # 503 path
      try:
          restored = ...restore...
      except Exception:
          lock.release(); raise
      return g, lock, restored
  ```
- Release on every exception path (one `try/finally` in `chat` covering
  from acquire success to response return — the current code releases in
  several places; consolidate to a single `finally`).

**Acceptance:** all slots busy 300 s → 503, then slots still usable
afterwards (no leaked locks — a test harness with fake clients).

---

## P7 — Observability + tests

- `/metrics` (prometheus text): requests, restore hits/misses,
  save ok/fail, meta count, per-slot table state, reader task count.
  (The router's own `--metrics` passes through separately — no collision,
  different path.)
- Tests (zero today):
  - `hashing.py` pure functions (trivial, do first).
  - `SlotManager` with fake clients (acquire/release/evict/restore
    branches — covers P4/P6 directly).
  - `llama_client` against a stubbed httpx transport (router body-injection
    for P0 — the exact 400 repro).
  - Stream-reader lifecycle (P5) with a fake chunk source + disconnect.

---

## P8 — Re-align scope with native llama.cpp features (product decision)

The live server already runs `--cache-idle-slots --cache-prompt` and the
current llama.cpp adds `--slot-prompt-similarity` (server-side slot reuse,
default 0.10), `--cache-ram` (RAM KV pool, default 8 GiB),
`-ctxcp/--ctx-checkpoints`. Decide which of the proxy's jobs the server now
does natively and *delete* the proxy version:

| Proxy job | Native counterpart | Verdict |
|---|---|---|
| Post-request save | `--cache-idle-slots` (idle→prompt cache) | overlaps; keep proxy save only for *keyed* persistence |
| Restore targeting | `--slot-prompt-similarity` | keep proxy (LCP over blocks is stronger, cross-restart) |
| Slot pinning | server auto-assigns | keep (needed for restore semantics) |
| Multi-backend fan-out | router does per-model | proxy can thin to "watcher over router(s)" |

Target end-state: proxy = **watcher** (P1's interceptors) + **LCP meta
index** (P3) + **pre-eviction save** (P4). Everything else is
pass-through. That is the minimum maintain surface that still justifies
the proxy's existence.

---

## A1 — Cache-reuse receipts (self-correcting index)

**Why:** the proxy's LCP match is a *prediction*; today nothing checks
whether llama.cpp actually reused the KV, so the meta index rots silently.

**Change:** on every response (non-stream: `timings.cache_n` /
`usage.prompt_tokens_details.cached_tokens`; stream: parse the final SSE
chunk), record tokens-reused per meta key. Running reuse score per key:
- reused ≈ 0 for a "matched" key → mark stale, evict at next GC (P3);
- high reuse → confirm, bump LRU.
Also feeds the hit-rate metric (P7).

**Acceptance:** corrupt/absent `.bin` for a key → next "hit" for that key
lands cold once, then the key is pruned from the index.

---

## SV — Save policy: single slot + swappable model

Deployment fact: **1 slot, router `--models-max 1`** — the model can be
swapped at any time, which kills the slot's KV. Disk save is therefore
the *only* persistence; save policy flips from "save big requests
post-hoc" to "save on change, often, cheaply".

Rules (all fire on **change**, never per-request):

1. **Save on key change (pre-eviction, promoted from P4).** Before
   forwarding a request whose key ≠ the slot's current key and whose LCP
   ratio to it is below the reuse threshold, save the old key first. Pure
   extensions (ratio ≈ 1.0) skip the save — the old cache is a prefix.
2. **Always save before restore.** A restore overwrites the slot; with one
   slot this fires on every conversation switch (hottest path).
3. **Event/periodic save of the current key** — see EV section below.
4. **Growth-threshold save within a conversation.** If the slot's key
   prefix grew ≥ N tokens (default ~1–2k) since the last save of that
   key, save again. Bounds loss on crash/unload of an open conversation.
5. **Cost fuse.** Measured: ~1.2 s / 1.76 GB at 76k tokens. Enforce a
   minimum interval (default ≥10 s) between saves of the same key; skip
   saves whose key equals the last-saved key.

Traffic-agnostic by construction: chat / tools / compression are all just
`messages` → keys. Tool round-trips extend the conversation (new key,
LCP ≈ 1.0 → restores correctly). A context-compaction rewrite breaks the
prefix → new key, old key pruned by A1 receipts. No special-casing.

**Consequences for other phases:** P4 becomes the core loop (not a
nicety); P3's cap is effectively "how many distinct conversations stay
warm" (~2–4 at 1.8 GB each); B1's table degenerates to one entry per model.

---

## EV — Event-driven save triggers (no proxy↔server bundling)

The proxy stays a sibling process (started/killed by the same bash script,
`LLAMA_URL` env discovery). All triggers use llama.cpp's public HTTP API:

1. **Model switch/unload — native event: `GET /models/sse`.** One
   background task subscribes to the router's model SSE stream
   (`model_status`: `loading`/`loaded`/`unloaded`/`sleeping`,
   `model_remove`):
   - `loading` for a model ≠ current → **pre-emptive save of the current
     key immediately** (the `--models-max 1` swap kills KV on unload;
     saving at `loading` beats the race).
   - `unloaded` → clear that model's slot table (keys become disk-only).
   - `loaded` → table starts empty; first request restores from disk.
   - Stream drops → reconnect with backoff; on reconnect, reconcile via
     polling (below) since events were missed.
2. **In-model KV loss (sleep mode, overflow, cache pressure) — poll.**
   No event exists; the reconciler (B2) every ~15–30 s does
   `GET /props?model=` (`is_sleeping`) + `GET /slots?model=`
   (`n_prompt_tokens`, `is_processing`). Detected drop → table entry
   marked cold.
3. **Proxy shutdown — the script already delivers it.** The launcher
   script's `trap cleanup SIGINT SIGTERM` sends SIGTERM to all jobs →
   uvicorn graceful shutdown → FastAPI `shutdown()` hook. **Add save of
   the current key to `app.shutdown()`.** SIGTERM is guaranteed for
   Ctrl+C/`kill`; only SIGKILL/crash escapes → bounded by rule 3/4 saves.
4. **Hard crash of llama — unknowable.** Layering is the answer:
   event save (exact, model switch) + growth/periodic save (bounds crash
   loss) + pre-restore save (every switch the proxy controls).

**Port plan (decided):** llama.cpp → **30000**, proxy → **30001**.
The proxy takes llama's old port, so existing clients (Open-WebUI etc.)
keep pointing at 30001 with no changes. Script shape stays: env-export +
sibling start + shared trap (deferred — script adjusts after the repo
upgrades land).

---

## Sequencing & rough size

Canonical path (solid under intensive use): 1) P0 → 2) A1 → 3) B1+B3
(table + verified save/restore) → 4) P5+P6 (load invariants) → 5) B2/EV
(reconcile + event triggers) → 6) A2 (apply-template hashing) → 7) P1 +
native `--slot-prompt-similarity` → 8) P3, B4, SV rules, P7.

| Phase | Depends on | Size | Risk if skipped |
|---|---|---|---|
| P0 | — | ~20 LOC | save/restore dead; 500s on success |
| A1 | P0 | ~15 LOC | index rots silently |
| B1 (table + pre-save) | P0 | ~40 LOC | phantom keys; silent cache loss |
| B3 (verify save/restore) | B1 | ~30 LOC | phantom keys survive |
| P5 | — | ~50 LOC | slot lock leaks under load |
| P6 | — | ~30 LOC | permanent slot loss |
| B2 + EV (reconcile + SSE) | B1 | ~100 LOC | foreign traffic & model swaps lose KV |
| A2 (apply-template hashing) | A1 | ~80 LOC | optimistic matches (roles/tools) |
| P1 (pass-through) | P0 | ~200 LOC rewrite of app.py | endpoint drift, model-id faking |
| P2 (slot discovery) | P1 | ~60 LOC | wrong slot tables (moot at 1 slot) |
| P3 (meta index + GC) | A1 | ~100 LOC | latency/disk growth |
| SV (save policy rules) | B1, EV | ~60 LOC | loss window unbounded |
| B4 (restart survival) | B2 | ~20 LOC | table empty after proxy restart |
| P7 (metrics + tests) | all | tests + /metrics | regression blindness |
| P8 (scope vs native) | P1–P4 | design time | scope creep |

---

## Final step — repo revamp (after all upgrades land)

Goal: the repo becomes the source of truth — tidy code + docs that match
real behavior.

- **Code:** one clean module layout (config / hashing / meta store /
  slot table / event watcher / passthrough app), no dead code
  (`touch_meta`), no duplicated save/restore logic, type hints, focused
  unit tests per module (hashing, meta store, slot table, save policy).
- **README.md / readme_RU.md:** rewrite to describe *what the proxy
  actually does now* — transparent pass-through + keyed KV persistence;
  drop the "saves before eviction" claim until it's true (it will be);
  document config, ports (llama 30000 / proxy 30001), and the save
  policy rules; one canonical English README, RU as a translation pass.
- **Docs in-repo:** keep `assessment.md`/`upgrade.md` as the design
  record; add a short `ARCHITECTURE.md` diagram (request flow, save
  triggers, event loop) so future changes have a map.
- **Definition of done:** README sentence-by-sentence verifiable against
code; every documented config knob exists in `config.py`; tests green;
  no feature documented that isn't implemented.

P0 + P5 + P6 are bug fixes (can land behind P1); P1 is the reframe; P2–P4
are the value add; P7/P8 keep it there.
