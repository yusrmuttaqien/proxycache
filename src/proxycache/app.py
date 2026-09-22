# app.py

# -*- coding: utf-8 -*-

"""
Simple KV proxy.

- Big requests: LCP match -> restore, chat pinned to that slot, then save + meta.
- Small requests: free/oldest slot, no restore, no disk save/meta.
- Slot pin is duplicated in body root / options / query (see llama_client).

Notes:
- acquire_for_request is wrapped in a timeout so a stuck slot cannot hang the app.
- Save/restore failures are non-fatal (P0): a failed save never breaks the chat.
- Shutdown (SIGTERM): save the current key of every occupied slot.
- Stream: reading from llama.cpp runs in a background reader task; the reader
  pushes chunks into an asyncio.Queue; in its finally it always does
  save_after + write_meta + release, then puts a None sentinel; the
  StreamingResponse only reads the queue and never affects slot release.
"""

import asyncio
import json
import os
import re
import time
import logging
from contextlib import asynccontextmanager
from typing import List, Dict, AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .config import (
    ACQUIRE_TIMEOUT,
    BACKENDS,
    BIG_THRESHOLD_WORDS,
    BIG_THRESHOLD_TOKENS,
    LCP_TH,
    META_DIR,
    MODELS,
    EVENT_SAVES,
    SAVE_MIN_INTERVAL,
    SAVE_PATH,
    TOKENS_PER_BLOCK,
    WORDS_PER_BLOCK,
    PORT,
)
from . import hashing as hs
from .llama_client import LlamaClient
from .meta_index import MetaIndex
from .receipts import Receipts
from .slot_manager import SlotManager, GSlot
from .watcher import ModelWatcher

log = logging.getLogger(__name__)

STREAM_QUEUE_SIZE = 16

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Build state, yield, then stop watchers, save occupied slots, close clients."""
    await _startup()
    yield
    await _shutdown()


app = FastAPI(title="Simple KV Proxy", lifespan=lifespan)

# P7: plain counters (no external deps)
METRICS = {
    "requests_total": 0,
    "big_requests_total": 0,
    "restore_attempts_total": 0,
    "restore_ok_total": 0,
    "save_attempts_total": 0,
    "save_ok_total": 0,
    "tokens_reused_total": 0,
}


def _count(k: str, v: int = 1):
    METRICS[k] += v


@app.get("/metrics")
async def metrics():
    lines = [f"proxycache_{k} {v}" for k, v in METRICS.items()]
    return "\n".join(lines) + "\n"


HOP_BY_HOP = {
    "host", "content-length", "connection", "transfer-encoding",
    "keep-alive", "te", "trailer", "upgrade",
}


async def _preunload_save(raw: bytes) -> None:
    """POST /models/unload: persist the slot's current key before the KV dies.

    Converts the graceful-unload case from bounded loss to zero loss; the
    saved state is what the next chat restores. Skip when the slot is busy
    (same tradeoff as the watcher's preemptive save).
    """
    try:
        m = json.loads(raw).get("model")
    except Exception:
        return
    if not m:
        return
    if not EVENT_SAVES:
        return
    sm: SlotManager = app.state.sm
    for g in sm._all_slots:
        if sm._slot_models.get(g) != m:
            continue
        key = sm.slot_key(g)
        if not key:
            return
        lock = sm._locks[g]
        if lock.locked():
            log.warning("preunload_save_skip_locked model=%s key=%s", m, key[:16])
            return
        await lock.acquire()
        try:
            ok = await sm.save_after(g, key, m)
            log.info("preunload_save model=%s key=%s ok=%s", m, key[:16], ok)
        finally:
            sm.release(g)
        return


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def passthrough(req: Request):
    """P1: byte-for-byte pass-through to the backend.

    Interception points:
    - POST /v1/chat/completions is a separate explicit route (the watch);
    - POST /slots/{id}?action=save|restore gets model injected into the body;
    - everything else (health, props, models, tools, Web UI, ...) is raw.
    """
    path = req.url.path

    # The watch: explicit route logic.
    if req.method == "POST" and path == "/v1/chat/completions":
        return await chat(req)

    clients = app.state.clients
    managed: List[str] = app.state.models
    client_model = managed[0] if managed else ""

    # /slots/{id}?action=save|restore — inject model (router requires it).
    if (
        req.method == "POST"
        and re.match(r"^/slots/\d+", path)
        and req.url.query in ("action=save", "action=restore")
    ):
        raw = await req.body()
        try:
            body = json.loads(raw)
            if "model" not in body:
                body["model"] = client_model
                raw = json.dumps(body).encode("utf-8")
        except Exception:
            pass
    elif req.method == "POST" and path == "/models/unload":
        raw = await req.body()
        try:
            await _preunload_save(raw)
        except Exception as e:
            log.warning("preunload_save_fail: %s", e)
    else:
        raw = await req.body()

    url = path + (f"?{req.url.query}" if req.url.query else "")
    headers = {
        k: v for k, v in req.headers.items() if k.lower() not in HOP_BY_HOP
    }

    http = clients[0].client
    try:
        response = await http.send(
            http.build_request(req.method, url, content=raw, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as e:
        log.warning("passthrough_backend_error %s %s: %s", req.method, path, e)
        return JSONResponse(
            {"error": f"backend unavailable: {e}"}, status_code=502
        )

    resp_headers = {
        k: v for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP
    }

    async def gen():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(gen(), status_code=response.status_code, headers=resp_headers)


async def _startup():
    clients = [LlamaClient(be["url"]) for be in BACKENDS]

    # Tip-keeping: resolve the llama host's --slot-save-path (config override,
    # else auto-detect from GET /models) so superseded .bin files can be removed.
    save_path = SAVE_PATH
    if not save_path and clients:
        save_path = await clients[0].get_slot_save_path()
    app.state.save_path = save_path
    log.info("save_path %s", save_path or "(none — .bin tip-keeping disabled)")

    # Model resolution: config list wins; empty = auto-discover all.
    models: List[str] = list(MODELS)
    if not models:
        models = await clients[0].get_models()
        log.info("models_autodiscover %s", models)
    if not models:
        log.warning("models_empty — router save/restore injection disabled")
    app.state.models = models

    # P2: slot discovery — the server's /slots is the truth, config is fallback.
    for be_id, client in enumerate(clients):
        for m in models:
            slots = await client.get_slots(model=m)
            if slots:
                n = len(slots)
                if n != int(BACKENDS[be_id]["n_slots"]):
                    log.info(
                        "slot_discovery be=%d model=%s cfg_n_slots=%d server_n_slots=%d",
                        be_id,
                        m,
                        BACKENDS[be_id]["n_slots"],
                        n,
                    )
                BACKENDS[be_id]["n_slots"] = n
                break

    sm = SlotManager()
    sm.set_clients(clients)
    app.state.clients = clients
    app.state.sm = sm
    app.state.receipts = Receipts()
    app.state.index = MetaIndex()

    # B2/EV: model lifecycle watcher per backend (all managed models).
    watchers: List[ModelWatcher] = []
    for be_id, client in enumerate(clients):
        w = ModelWatcher(be_id, client, sm, models=models)
        watchers.append(w)
        await w.start()
    app.state.watchers = watchers
    log.info(
        "app_start n_backends=%d models=%s port=%d",
        len(BACKENDS), models, PORT,
    )


async def _shutdown():
    for w in getattr(app.state, "watchers", []):
        w.stop()
    sm: SlotManager = getattr(app.state, "sm", None)
    if sm is not None:
        try:
            if EVENT_SAVES:
                await sm.shutdown_save()
        except Exception as e:
            log.warning("shutdown_save_exception: %s", e)
    clients: List[LlamaClient] = getattr(app.state, "clients", [])
    if clients:
        await asyncio.gather(*(c.close() for c in clients))


def _supersede_tip(old_key: str) -> None:
    """Remove a key whose cache/.meta.json is strictly implied by a newer
    pure-extension save (tip-keeping: a growing chat keeps one rolling cache).

    llama writes the cache file named exactly ``{key}`` (the filename we
    pass); ``{key}.bin`` is kept as a fallback for older probe files.
    """
    index: MetaIndex = app.state.index
    save_path: str = app.state.save_path
    index.remove(old_key)
    paths = []
    if save_path:
        paths.append(os.path.join(save_path, old_key))
        paths.append(os.path.join(save_path, f"{old_key}.bin"))
    paths.append(os.path.join(META_DIR, f"{old_key}.meta.json"))
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("tip_superseded key=%s path=%s", old_key[:16], path)
        except Exception as e:
            log.warning("tip_supersede_fail key=%s path=%s: %s", old_key[:16], path, e)


async def start_stream_task(
    resp: httpx.Response,
    g: GSlot,
    key: str,
    prefix: str,
    blocks: List[str],
    wpb: int,
    unit: str,
    model_id: str,
    route_model: str,
    sm: SlotManager,
    tip_old: Optional[str] = None,
) -> AsyncGenerator[bytes, None]:
    queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=STREAM_QUEUE_SIZE)
    receipts: Receipts = app.state.receipts

    async def reader():
        last_json: Optional[Dict] = None
        try:
            log.info("stream_reader_start g=%s key=%s", g, key[:16])
            async for chunk in resp.aiter_raw():
                if not chunk:
                    continue
                # A1: find the last SSE JSON chunk carrying timings.
                for line in chunk.decode("utf-8", "ignore").splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            try:
                                obj = json.loads(payload)
                                if isinstance(obj, dict) and ("timings" in obj or "usage" in obj):
                                    last_json = obj
                            except Exception:
                                pass
                try:
                    await queue.put(chunk)
                except asyncio.CancelledError:
                    log.warning("stream_reader_cancelled_put g=%s key=%s", g, key[:16])
                    raise
        except asyncio.CancelledError:
            log.warning("stream_reader_cancelled g=%s key=%s", g, key[:16])
            raise
        except Exception as e:
            log.exception("stream_reader_error g=%s key=%s: %s", g, key[:16], e)
        finally:
            # A1: record the KV reuse receipt.
            if last_json is not None:
                try:
                    timings = last_json.get("timings") or {}
                    cache_n = int(timings.get("cache_n") or 0)
                    prompt_n = int(timings.get("prompt_n") or 0)
                    _count("tokens_reused_total", cache_n)
                    receipts.record(key, cache_n, prompt_n)
                    if receipts.is_stale(key):
                        receipts.prune_stale([key])
                except Exception as e:
                    log.warning("receipt_stream_fail key=%s: %s", key[:16], e)
            try:
                await resp.aclose()
            except Exception:
                pass
            ok = False
            if sm.save_allowed(key, SAVE_MIN_INTERVAL):
                try:
                    ok = await sm.save_after(g, key, route_model, len(prefix))
                except Exception as e:
                    log.warning("save_after_exception g=%s key=%s: %s", g, key[:16], e)
            else:
                log.info("save_skip_fuse key=%s", key[:16])
            # Tip-keeping: the new key is a pure extension of tip_old — the
            # old snapshot is strictly redundant now that this save succeeded.
            if ok and tip_old:
                _supersede_tip(tip_old)
            try:
                app.state.index.write(key, prefix, blocks, wpb, model_id, unit)
            except Exception as e:
                log.warning("write_meta_exception key=%s: %s", key[:16], e)
            sm.release(g)
            log.info("stream_reader_done g=%s key=%s saved=%s", g, key[:16], ok)
            try:
                await queue.put(None)
            except Exception:
                pass

    reader_task = asyncio.create_task(reader())

    async def gen() -> AsyncGenerator[bytes, None]:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            # P5: client disconnect / response close must not leave the
            # reader (and the slot lock) behind.
            if not reader_task.done():
                reader_task.cancel()

    return gen()


async def chat(req: Request):
    sm: SlotManager = app.state.sm
    clients: List[LlamaClient] = app.state.clients

    t0 = time.time()
    _count("requests_total")
    data = await req.json()

    messages: List[Dict] = data.get("messages") or []
    stream = bool(data.get("stream", False))
    managed: List[str] = app.state.models
    client_model = data.get("model") or (managed[0] if managed else "")

    # Key identity includes the request's own model (multi-model safe).
    backend_model_id = client_model

    # A2: hash what the model actually sees — server-rendered prompt
    # tokenized into token blocks. Falls back to raw word blocks when
    # /apply-template or /tokenize is unavailable.
    rendered = await clients[0].apply_template(data)
    if rendered is not None:
        tokens = await clients[0].tokenize(rendered, client_model)
        if tokens:
            prefix = rendered
            blocks = hs.token_blocks_from_ids(tokens, TOKENS_PER_BLOCK)
            wpb = TOKENS_PER_BLOCK
            unit = "tokens"
            is_big = len(tokens) > BIG_THRESHOLD_TOKENS
        else:
            prefix = hs.raw_prefix(messages)
            blocks = hs.block_hashes_from_text(prefix, WORDS_PER_BLOCK)
            wpb = WORDS_PER_BLOCK
            unit = "words"
            is_big = len(hs.words_from_text(prefix)) > BIG_THRESHOLD_WORDS
    else:
        prefix = hs.raw_prefix(messages)
        blocks = hs.block_hashes_from_text(prefix, WORDS_PER_BLOCK)
        wpb = WORDS_PER_BLOCK
        unit = "words"
        is_big = len(hs.words_from_text(prefix)) > BIG_THRESHOLD_WORDS

    full_for_key = backend_model_id + "\n" + prefix
    key = hs.prefix_key_sha256(full_for_key)

    index: MetaIndex = app.state.index
    if is_big:
        _count("big_requests_total")
    restore_key: Optional[str] = None
    if is_big:
        cand = index.find_best(
            blocks,
            wpb,
            LCP_TH,
            backend_model_id,
            unit,
        )
        if cand:
            restore_key, ratio = cand
            log.info(
                "restore_candidate basename=%s ratio=%.3f",
                restore_key[:16],
                ratio,
            )
        else:
            log.info("restore_candidate none")
    else:
        log.info(
            "small_request unit=%s n_units~%d",
            unit,
            len(blocks) * wpb,
        )

    log.info(
        "before_acquire is_big=%s unit=%s n_blocks=%d restore_key=%s",
        is_big,
        unit,
        len(blocks),
        restore_key[:16] if restore_key else None,
    )

    g = await sm.acquire(acquire_timeout=ACQUIRE_TIMEOUT)
    if g is None:
        log.error(
            "acquire_timeout is_big=%s restore_key=%s",
            is_big,
            restore_key[:16] if restore_key else None,
        )
        return JSONResponse(
            {"error": "all slots busy, please retry later"},
            status_code=503,
        )

    # SV pre-eviction save: the slot holds a DIFFERENT key and it is not a
    # pure prefix of the new one -> persist the old key before we overwrite.
    cur_key = sm.slot_key(g)
    cur_meta = index.get(cur_key) if cur_key else None
    # tip_old: the key this request extends — its .bin is superseded once
    # this request's own save succeeds (tip-keeping).
    tip_old: Optional[str] = None
    if cur_key and cur_key != key and cur_meta:
        ext = False
        if cur_meta.get("unit", "words") == unit:
            cur_blocks = cur_meta.get("blocks") or []
            lcp = hs.lcp_blocks(blocks, cur_blocks)
            denom = max(1, min(len(blocks), len(cur_blocks)))
            ext = (lcp / denom >= LCP_TH) and len(cur_blocks) <= len(blocks)
        if ext:
            tip_old = cur_key
        if not ext and sm.save_allowed(cur_key, SAVE_MIN_INTERVAL):
            log.info("pre_save g=%s old_key=%s", g, cur_key[:16])
            try:
                await sm.save_after(
                    g, cur_key, sm._slot_models.get(g),
                    prefix_len=cur_meta.get("prefix_len", 0),
                )
            except Exception as e:
                log.warning("pre_save_exception g=%s key=%s: %s", g, cur_key[:16], e)

    if cur_key == key:
        index.touch_used(key)  # slot already holds this key: a reuse hit

    restored: Optional[bool] = None
    if is_big and restore_key:
        _count("restore_attempts_total")
        restored = await sm.restore(g, restore_key, client_model)
        if restored:
            _count("restore_ok_total")
            index.touch_used(restore_key)

    log.info("after_acquire g=%s restored=%s cur_key=%s", g, restored,
             cur_key[:16] if cur_key else None)

    be_id, slot_id = g
    client = clients[be_id]

    body = dict(data)
    body["model"] = client_model
    body["cache_prompt"] = bool(is_big)
    body["n_keep"] = -1

    opts = dict(body.get("options") or {})
    opts["slot_id"] = slot_id
    opts["id_slot"] = slot_id
    opts["n_keep"] = -1
    opts["cache_prompt"] = bool(is_big)
    body["options"] = opts

    log.info(
        "dispatch be=%d slot=%d is_big=%s (restore_target=%s restored=%s model_id=%s)",
        be_id,
        slot_id,
        is_big,
        restore_key[:16] if restore_key else None,
        restored,
        backend_model_id,
    )

    try:
        if stream:
            resp = await client.chat_completions(
                body,
                slot_id=slot_id,
                stream=True,
            )
            if resp.status_code != 200:
                err_txt = await resp.aread()
                await resp.aclose()
                sm.release(g)
                return JSONResponse(
                    {"error": err_txt.decode("utf-8", "ignore")},
                    status_code=resp.status_code,
                )

            gen = await start_stream_task(
                resp,
                g,
                key,
                prefix,
                blocks,
                wpb,
                unit,
                backend_model_id,
                client_model,
                sm,
                tip_old,
            )

            headers = {
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
            return StreamingResponse(
                gen,
                media_type="text/event-stream",
                headers=headers,
            )

        else:
            out = await client.chat_completions(
                body,
                slot_id=slot_id,
                stream=False,
            )
            if not isinstance(out, dict):
                sm.release(g)
                return JSONResponse(
                    {"error": "provider non-JSON body"},
                    status_code=502,
                )

            receipts: Receipts = app.state.receipts
            index: MetaIndex = app.state.index
            timings = out.get("timings") or {}
            cache_n = int(timings.get("cache_n") or 0)
            _count("tokens_reused_total", cache_n)
            try:
                receipts.record(
                    key,
                    cache_n,
                    int(timings.get("prompt_n") or 0),
                )
                if receipts.is_stale(key):
                    receipts.prune_stale([key])
                    index.remove(key)
            except Exception as e:
                log.warning("receipt_fail key=%s: %s", key[:16], e)

            ok = False
            if is_big:
                # SV cost fuse: never re-save the same key more often than
                # SAVE_MIN_INTERVAL (bounds crash loss without churning disk).
                if sm.save_allowed(key, SAVE_MIN_INTERVAL):
                    _count("save_attempts_total")
                    try:
                        ok = await sm.save_after(g, key, client_model, len(prefix))
                        if ok:
                            _count("save_ok_total")
                    except Exception as e:
                        log.warning("save_after_exception g=%s key=%s: %s", g, key[:16], e)
                else:
                    log.info("save_skip_fuse key=%s", key[:16])
                if ok and tip_old:
                    _supersede_tip(tip_old)
                try:
                    index.write(
                        key,
                        prefix,
                        blocks,
                        wpb,
                        backend_model_id,
                        unit,
                    )
                except Exception as e:
                    log.warning("write_meta_exception key=%s: %s", key[:16], e)
            sm.release(g)

            log.info(
                "json_done g=%s key=%s saved=%s is_big=%s dur_ms=%d",
                g,
                key[:16],
                ok,
                is_big,
                int((time.time() - t0) * 1000),
            )
            return JSONResponse(content=out, status_code=200)

    except Exception as e:
        sm.release(g)
        log.exception("chat_error g=%s key=%s: %s", g, key[:16], e)
        return JSONResponse({"error": str(e)}, status_code=500)
