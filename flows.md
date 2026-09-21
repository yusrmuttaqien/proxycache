# Proxycache — Flow Map

Every file, function, and branch in the repo, traced. Line refs are to the
files as of `main` (commit c0f7983). External API calls marked ✅/⚠️ are
verified against the live llama.cpp router at `yusrils-desktop.local:30001`
(model `27B-Q3.8`, 1 slot) and `ggml-org/llama.cpp` master `tools/server/README.md`.

```
proxycache.py ──import──▶ app.py ──import──▶ config.py
       │                   │  ├──▶ hashing.py
       │                   │  ├──▶ llama_client.py ──▶ config.REQUEST_TIMEOUT
       │                   │  └──▶ slot_manager.py ──▶ config.BACKENDS
       └── uvicorn.run(app)
```

---

## 1. `proxycache.py` (entry point)

- Imports `app`, `PORT`, `LOG_LEVEL`.
- `if __name__ == "__main__"`: `uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL.lower())`.
- No other code. Can also be started via `uvicorn app:app` (README).

## 2. `config.py` (module import side effects)

- `BACKENDS_RAW = os.getenv("BACKENDS")`
  - **Branch A** (env set): `json.loads`; on `Exception` → `BACKENDS = []`
    (silent failure → `app.py` `startup` builds zero clients; `clients[0]`
    in `chat()` then raises `IndexError` → 500 on every request).
  - **Branch B** (env unset): one backend
    `{url: env LLAMA_URL default http://127.0.0.1:8000, n_slots: env N_SLOTS default 2}`.
- `WORDS_PER_BLOCK` (env, default 100), `BIG_THRESHOLD_WORDS` (default 500),
  `LCP_TH` (default 0.6), `REQUEST_TIMEOUT` (default 600 s), `MODEL_ID`
  (default "llama.cpp"), `PORT` (default 8081), `LOG_LEVEL` (default INFO).
- `META_DIR = join(getcwd(), env META_DIR default "kv_meta")`;
  `os.makedirs(..., exist_ok=True)` — **side effect at import time**.
- `logging.basicConfig(level, format)` — **side effect at import time**;
  note `proxycache.py` imports `app` before any logging config, so order
  matters only if someone imports `app` alone.

## 3. `hashing.py`

### `raw_prefix(messages) -> str`
- Iterates `messages or []` (None-safe).
- Per msg: `content = msg.get("content", "")`
  - **Branch**: `isinstance(content, str)` → `content.strip()`.
  - **Branch**: else → `str(content).strip()` (list content — multimodal
    parts — stringified wholesale; roles are **dropped**).
- **Branch**: `if content:` append. Join with `"\n\n"`, `.strip()`, return.
- `log.debug` len.

### `words_from_text(text) -> List[str]`
- `re.findall(r"\w+", text.lower())` — unicode word chars, lowercased.

### `block_hashes_from_text(text, wpb=100) -> List[str]`
- Words → chunks of `wpb` → `" ".join(chunk)` → `sha256(...).hexdigest()`.
- **Branch**: `len(words) == 0` → loop body never runs → `[]`.
- Returns list of hex digests (no block content).

### `lcp_blocks(b1, b2) -> int`
- Linear scan while equal, capped at `min(len)`. Returns count.

### `prefix_key_sha256(text) -> str`
- `sha256(text.encode("utf-8")).hexdigest()`. (Key = this of
  `model_id + "\n" + raw_prefix`.)

### `scan_all_meta() -> List[Dict]`
- `glob(META_DIR/*.meta.json)`, sorted by mtime desc.
- Per file: `open`/`json.load`
  - **Branch**: success → append.
  - **Branch**: `Exception` → `log.warning("scan_meta_fail")`, skip.
- Returns list (all models, all wpb — filtering happens later).
- ⚠️ sync file I/O, called from the event loop.

### `find_best_restore_candidate(req_blocks, wpb, th, model_id) -> Optional[Tuple[str, float]]`
- `metas = scan_all_meta()`.
- Per meta:
  - **Branch**: `meta.get("model_id") != model_id` → `continue`.
  - **Branch**: `int(meta.get("wpb") or 0) != wpb` → `continue`.
  - `lcp = lcp_blocks(req_blocks, cand_blocks)`;
    `denom = max(1, min(len(req), len(cand)))`; `ratio = lcp/denom`.
  - **Branch**: `ratio >= th and ratio > best_ratio` → update best.
- **Return branch**: `(best_key, best_ratio)` if a best was found,
  else `None`.

### `write_meta(key, prefix_text, blocks, wpb, model_id) -> None`
- Builds dict `{key, model_id, prefix_len, wpb, blocks, timestamp}`.
- Writes `{META_DIR}/{key}.meta.json` (indent, ensure_ascii=False).
- **No** exception handling — disk errors propagate to caller.

### `touch_meta(key) -> None`
- `open(path, "r+")`:
  - **Branch A**: `json.load` fails → warn, return (file untouched).
  - Success: set `timestamp`, `seek(0)`, dump, `truncate()`.
- **Branch B**: `FileNotFoundError` → warn `touch_meta_missing`.
- **Branch C**: other `Exception` → warn `touch_meta_fail`.
- 🔴 **Dead code — never called anywhere in the repo.**

## 4. `llama_client.py`

### `LlamaClient.__init__(base_url)`
- `base_url.rstrip("/")`; `httpx.Limits(keepalive 20, conn 100)`.
- `httpx.AsyncClient(base_url, timeout=REQUEST_TIMEOUT, limits)`.
- Log `client_init` with httpx version.

### `close()`
- `await self.client.aclose()`.

### `_with_slot_id(body, slot_id) -> (body, query)`
- **Branch**: `slot_id is None` → `(body, {})` (no pinning at all).
- Else: copies body; sets root `_slot_id`, `slot_id`, `id_slot` = slot_id;
  copies `options` (or `{}`) and sets `slot_id`, `id_slot` inside;
  returns `query = {"slot_id": ..., "id_slot": ...}`.
- Verified: `id_slot` in body is the documented llama.cpp pin
  (`/completion` option "assign the completion task to a specific slot");
  the extra keys are defensive legacy (older builds accepted `options`),
  accepted-and-ignored by current builds.

### `chat_completions(body, slot_id=None, stream=False)`
- `body2, query = _with_slot_id(...)`.
- **Branch STREAM**:
  - `client.build_request("POST", "/v1/chat/completions", json=body2, params=query)`.
  - `client.send(req, stream=True)` → returns the raw `httpx.Response`
    (no status check here; caller checks).
- **Branch NON-STREAM**:
  - `client.post(...)` → `resp.raise_for_status()`
    (any 4xx/5xx raises `HTTPStatusError` → propagates to `chat()` outer
    `except` → 500 to client).
  - **Branch**: `"application/json" not in content-type` → log error,
    return `{"object":"error","message":"provider returned non-JSON","raw": first 2048}`.
  - `resp.json()`:
    - **Branch**: parse `Exception` → log, return
      `{"object":"error","message":"invalid json from provider","raw":...}`.
    - Success → dict.

### `save_slot(slot_id, basename) -> bool`
- `POST /slots/{slot_id}?action=save`, JSON `{"filename": basename}`.
  (JSON body required — "500 parse error on some builds" per docstring.)
- **Branch**: `status_code == 500` → warn, `return False`.
- **Else**: `raise_for_status()` (⚠️ 400 from router mode →
  `HTTPStatusError` **not** caught here) → `return True`.
- ✅ Verified on live server: `{"filename":"proxycache_probe.bin"}` →
  `{"id_slot":0,"filename":"proxycache_probe.bin","n_saved":76321,
  "n_written":1763631955,"timings":{"save_ms":1218.063}}`.
- ⚠️ Verified: in router mode the proxy's exact call 400s
  (`model name is missing`); adding `model` to the JSON body fixes it
  (query param does **not** work).

### `restore_slot(slot_id, basename) -> bool`
- `POST /slots/{slot_id}?action=restore`, JSON `{"filename": basename}`.
- **Branch**: `status_code != 200` → warn, `return False` (no
  `raise_for_status` here — asymmetric with save_slot).
- **Else** `return True`.
- Same router-mode 400 applies (not probed: would clobber the live slot).

### `get_model_id() -> str`
- `GET /v1/models`; `raise_for_status()`; `data["data"][0]["id"]`.
  - **Branch**: empty/odd list → `"unknown"`.
  - **Branch**: any `Exception` → warn, `"unknown"`.
- ✅ Verified: router forwards it; returned `27B-Q3.8`.
- Used only for internal cache keys; client-facing model id stays `MODEL_ID`.

## 5. `slot_manager.py`

### `SlotManager.__init__`
- Per `BACKENDS[i]`: `n_slots = int(conf["n_slots"])`; backend record
  `{id, client: None, n_slots}`; `total_slots += n_slots`.
- `_all_slots = [(be_id, s) for ...]` — Cartesian of backends × local slots.
- `_last_used: {g: 0.0}`, `_locks: {g: asyncio.Lock()}`.
- ⚠️ No validation that `n_slots` matches the server (live server has 1).

### `set_clients(clients)`
- Zip: `backends[i]["client"] = clients[i]`. (Silent mismatch if lengths
  differ — zip stops at the shorter.)

### `_is_free(g) -> bool`
- `self._last_used.get(g, 0.0) == 0.0` — "free" == "never had a successful
  save". (See assessment #4.)

### `_get_free_or_oldest() -> (g, lock)`
- **Branch A**: any free → first one in `_all_slots` order.
- **Branch B**: none → `sorted(..., key=_last_used)[0]` (true LRU oldest).

### `acquire_for_request(restore_key=None) -> (g, lock, restored|None)`
- `g, lock = _get_free_or_oldest()`.
- `await lock.acquire()` — **blocks until free** (this is where 300 s
  `ACQUIRE_TIMEOUT` in app.py bites; see assessment #9).
- **Branch**: `restore_key` set:
  - `client = backends[g[0]]["client"]`
  - `restored = await client.restore_slot(g[1], restore_key)`
    - ⚠️ In router mode this 400s → `restore_slot` returns `False`
      (status != 200 branch) → restore silently ineffective, request
      proceeds with a **cold** slot (correct fallback, but the "restore"
      optimization never fires).
  - Log `restore_before_chat`.
- **Branch**: no `restore_key` → `restored = None`.
- Returns `(g, lock, restored)`. Note: lock is returned already held.

### `save_after(g, key) -> bool`
- `ok = await client.save_slot(g[1], key)`
  - ⚠️ Router mode: raises `HTTPStatusError` before returning.
- `self._last_used[g] = time.time()` (only on success).
- `return ok`.

### `release(g)`
- **Branch**: `self._locks[g].locked()` → `release()`.
- **Branch**: not locked → no-op (idempotent).

## 6. `app.py`

### Module level
- `ACQUIRE_TIMEOUT = 300.0`, `STREAM_QUEUE_SIZE = 16`,
  `app = FastAPI(title="Simple KV Proxy")`.

### `startup()` (`@app.on_event("startup")`)
- `clients = [LlamaClient(be["url"]) for be in BACKENDS]`.
- `sm = SlotManager(); sm.set_clients(clients)`.
- `app.state.clients / app.state.sm`. Log `app_start`.
- ⚠️ If `BACKENDS == []` (bad JSON), `clients == []` → `chat()`
  `clients[0]` → `IndexError` → 500.

### `shutdown()`
- `clients = getattr(app.state, "clients", [])`
  - **Branch**: non-empty → `asyncio.gather(*(c.close() ...))`.
  - **Branch**: empty → skip.

### `GET /v1/models` → `models()`
- Static: `{"data": [{"id": MODEL_ID}]}` (no backend round-trip).

### `start_stream_task(resp, g, key, prefix, blocks, model_id, sm) -> AsyncGenerator`
- `queue = asyncio.Queue(maxsize=16)`.
- Defines `reader()` (async):
  - `log.info("stream_reader_start")`.
  - `async for chunk in resp.aiter_raw():`
    - **Branch**: `chunk` falsy → `continue`.
    - `await queue.put(chunk)`
      - **Branch**: `asyncio.CancelledError` → warn, re-raise
        (⚠️ queue full + client gone → blocks here forever; no finalizer
        hook — assessment #8).
  - `except CancelledError` → warn, raise.
  - `except Exception` → `log.exception("stream_reader_error")`
    (swallowed; generation proceeds to `finally`).
  - `finally` (always):
    1. `await resp.aclose()` (swallows its own exceptions).
    2. `ok = await sm.save_after(g, key)` — `Exception` → warn, `ok=False`.
    3. `hs.write_meta(key, prefix, blocks, WORDS_PER_BLOCK, model_id)` —
       `Exception` → warn.
    4. `sm.release(g)` (unconditional).
    5. Log `stream_reader_done` with saved flag.
    6. `await queue.put(None)` sentinel — `Exception` → pass
       (⚠️ if the consumer is gone, this put blocks before the swallow
       only catches non-asyncio errors; `put` itself can raise
       `QueueEmpty`-style only on close — a blocked put on a full queue
       never reaches an except).
- `asyncio.create_task(reader())` — fire-and-forget (no `done_callback`).
- Defines `gen()`:
  - `while True: item = await queue.get(); if item is None: break; yield item`.
- Returns `gen()` (a fresh async generator object — the reader is *not*
  tied to gen's lifecycle).

### `POST /v1/chat/completions` → `chat(req: Request)`

1. `t0 = time.time()`; `data = await req.json()`
   (⚠️ JSON parse errors → unhandled → FastAPI 500, no structured error).
2. `messages = data.get("messages") or []`; `stream = bool(data.get("stream", False))`;
   `client_model = data.get("model") or MODEL_ID`.
3. `backend_model_id = await clients[0].get_model_id()`
   (⚠️ always backend 0; extra round-trip per request; "unknown" fallback
   means all metas keyed by the same id when the backend is down →
   key collisions across backends).
4. `prefix = hs.raw_prefix(messages)`;
   `full_for_key = backend_model_id + "\n" + prefix`;
   `key = hs.prefix_key_sha256(full_for_key)`;
   `blocks = hs.block_hashes_from_text(prefix, WORDS_PER_BLOCK)`;
   `n_words = len(hs.words_from_text(prefix))`;
   `is_big = n_words > BIG_THRESHOLD_WORDS`.
5. **Branch BIG** (`is_big`):
   - `cand = hs.find_best_restore_candidate(blocks, WORDS_PER_BLOCK, LCP_TH, backend_model_id)`
   - **Branch**: `cand` truthy → `restore_key, ratio = cand`; log ratio.
   - **Branch**: `cand is None` → log "restore_candidate none".
   - ⚠️ O(all meta files) sync I/O on the event loop.
   **Branch SMALL**: log `small_request n_words=... threshold=...`
   (no meta scan; no restore; no save — small requests never touch disk).
6. Log `before_acquire is_big=... restore_key=...`.
7. `g, lock, restored = await asyncio.wait_for(sm.acquire_for_request(restore_key if is_big else None), timeout=300.0)`
   - **Branch**: `asyncio.TimeoutError` → log, `JSONResponse({"error": "all slots busy, please retry later"}, 503)`.
   - **Branch**: any other exception (e.g. `HTTPStatusError` from a
     save-path surprise) escapes to FastAPI → 500, **lock may leak**
     (assessment #9).
8. Log `after_acquire g=... restored=...`.
9. `be_id, slot_id = g`; `client = clients[be_id]`.
10. Body prep: `body = dict(data)`; `body["model"] = client_model`
    (⚠️ keeps the *client's* model name — required for router routing,
    verified working); `body["cache_prompt"] = bool(is_big)`;
    `body["n_keep"] = -1`;
    `opts = dict(body.get("options") or {})`; `opts["slot_id"] = slot_id`;
    `opts["id_slot"] = slot_id`; `opts["n_keep"] = -1`;
    `opts["cache_prompt"] = bool(is_big)`; `body["options"] = opts`.
    (Triple pinning: root keys added by `llama_client._with_slot_id`.)
11. Log `dispatch be=... slot=... is_big=... restore_target=... restored=... model_id=...`.
12. **Outer `try`** wrapping dispatch:

    **Branch STREAM** (`stream`):
    - `resp = await client.chat_completions(body, slot_id, stream=True)`.
    - **Branch**: `resp.status_code != 200`:
      - `err_txt = await resp.aread()`; `await resp.aclose()`;
        `sm.release(g)`;
        return `JSONResponse({"error": err_txt...}, status_code=resp.status_code)`.
    - `gen = await start_stream_task(resp, g, key, prefix, blocks, backend_model_id, sm)`
      (⚠️ `start_stream_task` is a plain async function — `await`-ing it
      creates the reader task and returns `gen()`; fine, but naming is
      misleading).
    - `headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}`.
    - `return StreamingResponse(gen, media_type="text/event-stream", headers=headers)`
      (chunk bytes passed through raw — SSE framing from llama.cpp is
      forwarded untouched).

    **Branch NON-STREAM**:
    - `out = await client.chat_completions(body, slot_id, stream=False)`.
    - **Branch**: `not isinstance(out, dict)`
      (i.e. the two provider-error dicts from `chat_completions` are
      dicts — so this branch is effectively **unreachable**; non-dict can't
      happen from `resp.json()`; ⚠️ the "provider non-JSON body" 502 never
      fires — the error dicts flow through as 200s with
      `{"object":"error",...}` instead):
      - `sm.release(g)`; 502 JSON.
    - `ok = False`
    - **Inner `try`**:
      - **Branch**: `is_big` → `ok = await sm.save_after(g, key)`
        (⚠️ router 400 → `HTTPStatusError` → outer `except` → **500 after
        successful generation**);
        `hs.write_meta(...)` (⚠️ unguarded — disk error also 500s a
        successful completion).
      - **Branch**: `not is_big` → no save, no meta.
    - **`finally`**: `sm.release(g)` (always).
    - Log `json_done g=... key=... saved=... is_big=... dur_ms=...`.
    - `return JSONResponse(content=out, status_code=200)`.

    **`except Exception`** (outer):
    - `sm.release(g)`; `log.exception("chat_error")`;
    - `return JSONResponse({"error": str(e)}, 500)`.

---

## End-to-end sequence (happy path, big request, restore hit)

```
client ──POST /v1/chat/completions (stream)──▶ app.chat
  1. req.json() → messages, stream, client_model
  2. clients[0].get_model_id()          ──GET /v1/models──▶ llama.cpp   (✅ 27B-Q3.8)
  3. raw_prefix → blocks → key; n_words > 500 → BIG
  4. scan_all_meta() → best LCP ≥ 0.6 → restore_key
  5. SlotManager.acquire_for_request(restore_key)
        ├─ free/oldest → (be, slot), lock.acquire()
        └─ client.restore_slot(slot, key) ──POST /slots/{s}?action=restore──▶ llama.cpp
  6. body: model=client_model, n_keep=-1, cache_prompt=true, slot pin ×3
  7. client.chat_completions(stream) ──POST /v1/chat/completions──▶ llama.cpp
  8. reader task: aiter_raw → queue.put(chunk)* → [finally: aclose,
     save_after ──POST /slots/{s}?action=save──▶ llama.cpp, write_meta,
     release, put(None)]
  9. StreamingResponse drains queue → client (SSE passthrough)
```

Small-request path: skip 4, no restore, skip save (reader's finally still
calls `save_after` + `write_meta` even for small requests — ⚠️ **in the
stream path, small requests DO save and DO write meta**; only the
non-stream path skips them. This asymmetry is a behavior bug: streaming
small chats pollute the meta dir and can even overwrite slot KV with a
tiny context that was sitting in a "free" slot.)

Wait — re-check: in `start_stream_task`, `finally` always calls
`sm.save_after(g, key)` and `hs.write_meta(...)` unconditionally. Yes —
the stream path saves for **every** request, big or small, while the
non-stream path saves only when `is_big`. Asymmetric, confirmed.

## Per-file branch coverage checklist

- [x] `proxycache.py` — 1 branch (`__main__`).
- [x] `config.py` — BACKENDS A/B, makedirs, logging side effects.
- [x] `hashing.py` — raw_prefix (str/else/empty), block loop, lcp loop,
      scan_all_meta (ok/fail), find_best (model/wpb/ratio), write_meta,
      touch_meta (A/B/C, dead).
- [x] `llama_client.py` — _with_slot_id (None/set), chat_completions
      (stream/non-stream/non-CTE/bad-JSON), save_slot (500/raise/ok),
      restore_slot (≠200/ok), get_model_id (ok/unknown/exception).
- [x] `slot_manager.py` — __init__, set_clients, _is_free,
      _get_free_or_oldest (free/oldest), acquire_for_request
      (restore_key/none, restore ok/fail), save_after, release
      (locked/not).
- [x] `app.py` — startup, shutdown (empty/full), models,
      start_stream_task (reader loop/put-cancel/reader-error/finally
      4-step, gen loop), chat (all 12 numbered steps, stream/non-stream,
      every status/error branch, acquire timeout, outer except).

Total: 7 Python files, ~700 LOC, all branches above traced. No other
source files exist (venv/ excluded; `requirements.txt` = pinned FastAPI
0.121 / httpx 0.28.1 / uvicorn 0.38 stack, Python 3.12).
