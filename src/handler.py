"""Request handler — the core of the proxy's request processing.

Intercepts chat requests: classify -> fingerprint -> conv key -> decide ->
execute -> stream. Passes through everything else.
(design: flow steps 1-8)
"""

from __future__ import annotations

import json
import logging

import aiohttp
from aiohttp import web

from src.client import UpstreamClient
from src.config import detect_save_paths, resolve_save_path
from src.convkey import ConvTracker
from src.desk import Desk
from src.routes import Action, classify
from src.allocator import SlotAllocator, inject_id_slot
from src.ledger import FileLedger
from src.save import should_save
from src.lru import LRUEvictor
from src.restore import RestoreAction, decide
from src.shifted import detect_shifted_suffix, should_inject_n_cache_reuse
from src.observability import echo_conversation_id

logger = logging.getLogger(__name__)

# Hop-by-hop headers (RFC 7230) that must not be forwarded.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})


def _strip_hop_by_hop(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


class ProxyState:
    """The proxy's shared state (per upstream)."""

    def __init__(self, upstream: str, config: dict):
        self.upstream = upstream
        self.config = config
        self.client = UpstreamClient(
            upstream,
            api_key=config.get("api_key", ""),
            timeout_ms=config.get("control_timeout_ms", 10000),
        )
        self.desk = Desk()
        self.tracker = ConvTracker(tail_len=config.get("tail_match_min", 64))
        self.save_paths: dict[str, str] = {}  # model -> save_path
        self.min_save_tokens = config.get("min_save_tokens", 512)
        self.ledger = FileLedger()
        self.lru = LRUEvictor(
            max_bytes=int(config.get("max_gb", 2.0) * (1024 ** 3)),
            n_max_files=config.get("n_max_files", 0),
        )
        self.slot_alloc = SlotAllocator(n_slots=config.get("n_slots", 1))
        self.saved_convs: set[str] = set()  # convs already saved
        self.prev_tokens: tuple[int, ...] | None = None  # previous conv's token list
        self.prev_conv_id: str | None = None  # previous conv's id

    async def initialize(self) -> None:
        """Startup: detect save_paths from /models."""
        models = await self.client.get_models()
        if models:
            self.save_paths = detect_save_paths(models)
            logger.info(f"save_paths: {self.save_paths}")

    def get_save_path(self, model: str) -> str | None:
        """Resolve the save_path for a model."""
        return resolve_save_path(
            model,
            self.config.get("save_path", ""),
            self.save_paths,
        )


async def forward(request: web.Request, state: ProxyState) -> web.StreamResponse:
    """Forward a request to upstream and stream the response back."""
    url = f"http://{state.upstream}{request.raw_path}"
    headers = _strip_hop_by_hop(request.headers)
    headers.pop("Host", None)
    body = await request.read()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method,
                url,
                headers=headers,
                data=body,
                timeout=aiohttp.ClientTimeout(total=None),
            ) as upstream_resp:
                response = web.StreamResponse(
                    status=upstream_resp.status,
                    headers=_strip_hop_by_hop(upstream_resp.headers),
                )
                await response.prepare(request)
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
    except aiohttp.ClientConnectionError:
        return web.Response(status=502, text="upstream unavailable")


async def handle_chat(request: web.Request, state: ProxyState) -> web.StreamResponse:
    """Handle a chat request: fingerprint -> conv key -> decide -> execute -> stream.

    (design: flow steps 1-8)
    """
    body = await request.read()

    # 1. Parse the body to get the model.
    try:
        data = json.loads(body)
        model = data.get("model", "")
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.debug("body parse fail — forwarding unmodified")
        return await _forward_bytes(request, state, body)

    # 2. Get the full token list (fingerprint).
    # Uses /apply-template + /tokenize (two-step) to get the exact token list.
    tokens = await state.client.get_token_list(body)
    if tokens is None:
        logger.warning("token_list failed — forwarding cold")
        return await _forward_bytes(request, state, body)
    
    token_count = len(tokens)
    logger.info(f"chat: model={model} tokens={token_count}")

    # 3. Determine the conversation key (using the full token list).
    conv_id = state.tracker.check(tokens, None)
    logger.info(f"conv={conv_id}")

    # 4. Allocate a slot for the conv.
    slot, evicted_conv = state.slot_alloc.allocate(conv_id)
    logger.debug(f"slot_alloc: conv={conv_id} -> slot={slot} evicted={evicted_conv}")

    # 5. Check if save is needed.
    # response_complete=True (we're calling after the response).
    # conv_in_slot=True (the conv is in the slot, since we just allocated it).
    # guard_on=True (the guard is on by default).
    save_needed = should_save(
        response_complete=True,
        l_tokens=token_count,
        min_save_tokens=state.min_save_tokens,
        conv_in_slot=True,
        guard_on=True,
    )
    logger.debug(f"save_needed: {save_needed} (L={token_count})")

    # 6. Inject id_slot into the body (always on).
    body = inject_id_slot(body, slot)

    # 6b. Detect shifted-suffix and inject n_cache_reuse (targeted mode).
    is_shifted = False
    if state.prev_tokens is not None:
        is_shifted = detect_shifted_suffix(state.prev_tokens, tokens)
        logger.debug(f"shifted-suffix: prev={state.prev_conv_id} curr={conv_id} is_shifted={is_shifted}")
    
    if should_inject_n_cache_reuse(is_shifted, targeted_mode=True):
        # Inject n_cache_reuse into the body.
        data = json.loads(body)
        data["n_cache_reuse"] = True
        body = json.dumps(data).encode()
        logger.info(f"n_cache_reuse injected: conv={conv_id}")
    
    # Update the prev_tokens for the next request.
    state.prev_tokens = tokens
    state.prev_conv_id = conv_id

    # 7. Check if restore is needed.
    restore_needed = conv_id in state.ledger
    logger.debug(f"restore_needed: {restore_needed} (conv in ledger: {conv_id in state.ledger})")

    # 8. If restore needed: execute the restore flow.
    if restore_needed:
        await _execute_restore(state, model, conv_id, slot)

    # 9. Forward with the modified body (echoing X-Conversation-Id).
    response = await _forward_bytes(request, state, body, conv_id)

    # 10. After response: execute the save flow.
    if save_needed:
        await _execute_save(state, model, conv_id, slot)

    return response


async def _execute_restore(state: ProxyState, model: str, conv_id: str, slot: int) -> None:
    """Execute the restore flow: decide -> drain -> save-first -> restore.

    (design: flow steps 4-5)
    """
    entry = state.ledger.get(conv_id)
    if entry is None:
        logger.debug(f"restore skipped: no ledger entry for conv={conv_id}")
        return

    # Decide the action.
    action = decide(state.desk, conv_id, True)  # saved=True (it's in the ledger)
    logger.debug(f"restore decision: conv={conv_id} action={action.value}")

    if action == RestoreAction.DRAIN:
        # Drain: save the current conv before restoring.
        logger.info(f"drain: conv={conv_id} (slot dirty)")
        # TODO: implement drain logic.
        return

    if action == RestoreAction.SAVE_FIRST:
        # Save-first: save before restore.
        logger.info(f"save-first: conv={conv_id}")
        # TODO: implement save-first logic.
        return

    if action == RestoreAction.RESTORE:
        # Restore: call the upstream restore endpoint.
        logger.info(f"restore: conv={conv_id} slot={slot} file={entry.path}")
        result = await state.client.restore_slot(slot, entry.path)
        if result is None:
            logger.warning(f"restore failed: conv={conv_id} slot={slot}")
            return
        # Update ram_since_restore.
        state.desk.ram_since_restore = conv_id
        logger.info(f"restored: conv={conv_id} slot={slot}")
        return

    if action == RestoreAction.FORWARD:
        logger.debug(f"forward: conv={conv_id} (no restore needed)")
        return


async def _execute_save(state: ProxyState, model: str, conv_id: str, slot: int) -> None:
    """Execute the save flow: HTTP call -> ledger update -> LRU eviction.

    (design: flow step 6)
    """
    save_path = state.get_save_path(model)
    if not save_path:
        logger.debug(f"save skipped: no save_path for model={model}")
        return

    # Build the filename: {conv_id}.bin
    filename = f"{conv_id}.bin"
    filepath = f"{save_path}/{filename}"

    # Call the upstream save endpoint.
    result = await state.client.save_slot(slot, filepath)
    if result is None:
        logger.warning(f"save failed: conv={conv_id} slot={slot}")
        return

    # Update the ledger.
    size = result.get("n_saved", 0)  # bytes saved
    state.ledger.set(conv_id, filepath, size)
    state.saved_convs.add(conv_id)
    logger.info(f"saved: conv={conv_id} slot={slot} size={size} bytes")

    # LRU eviction: check if over budget.
    evictions = state.lru.evict(state.ledger)
    for conv, path in evictions:
        state.ledger.remove(conv)
        logger.info(f"evicted: conv={conv} path={path}")
        # TODO: delete the file from disk.


async def _forward_bytes(
    request: web.Request,
    state: ProxyState,
    body: bytes,
    conv_id: str | None = None,
) -> web.StreamResponse:
    """Forward a request with a specific body to upstream.

    If conv_id is provided, echoes X-Conversation-Id in the response headers.
    """
    url = f"http://{state.upstream}{request.raw_path}"
    headers = _strip_hop_by_hop(request.headers)
    headers.pop("Host", None)
    # Update Content-Length to match the (possibly modified) body.
    headers["Content-Length"] = str(len(body))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method,
                url,
                headers=headers,
                data=body,
                timeout=aiohttp.ClientTimeout(total=None),
            ) as upstream_resp:
                resp_headers = _strip_hop_by_hop(upstream_resp.headers)
                # Echo X-Conversation-Id for client-side debugging.
                if conv_id is not None:
                    resp_headers["X-Conversation-Id"] = conv_id
                    logger.debug(f"echoed X-Conversation-Id: {conv_id}")
                response = web.StreamResponse(
                    status=upstream_resp.status,
                    headers=resp_headers,
                )
                await response.prepare(request)
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                return response
    except aiohttp.ClientConnectionError:
        return web.Response(status=502, text="upstream unavailable")


async def handle_request(request: web.Request, state: ProxyState) -> web.StreamResponse:
    """Handle one request: classify -> intercept/forward.

    (design: route table)
    """
    action = classify(
        request.method,
        request.path,
        str(request.query) if request.query else None,
    )

    if action == Action.INTERCEPT:
        logger.debug(f"INTERCEPT {request.method} {request.path}")
        return await handle_chat(request, state)

    if action == Action.PASS_DESK_DIRTY:
        logger.debug(f"PASS_DESK_DIRTY {request.method} {request.path}")
        # TODO: mark the desk dirty (invalidate ram_since_restore).
        return await forward(request, state)

    # PASS, PROXY_INTERNAL, or unknown -> forward.
    logger.debug(f"PASS {request.method} {request.path}")
    return await forward(request, state)


def make_handler(state: ProxyState):
    """Build the aiohttp handler bound to the proxy state."""
    async def handler(request: web.Request) -> web.StreamResponse:
        return await handle_request(request, state)
    return handler
