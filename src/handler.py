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
from src.allocator import inject_id_slot

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

    # 2. Get the input_tokens count (fingerprint).
    token_count = await state.client.get_input_tokens(body)
    if token_count is None:
        logger.warning("input_tokens failed — forwarding cold")
        return await _forward_bytes(request, state, body)

    # 3. Determine the conversation key.
    # NOTE: The endpoint returns the COUNT, not the LIST.
    # Using count as a basic fingerprint (sufficient for L >= min_save_tokens).
    # TODO: Use the full token list for a precise fingerprint.
    conv_id = state.tracker.check([token_count], None)  # count as single-element list
    logger.debug(f"chat: model={model} tokens={token_count} conv={conv_id}")

    # 4. Inject id_slot into the body (always on).
    slot = 0  # TODO: use the allocator to determine the slot.
    body = inject_id_slot(body, slot)

    # 5. Forward with the modified body.
    return await _forward_bytes(request, state, body)


async def _forward_bytes(
    request: web.Request, state: ProxyState, body: bytes
) -> web.StreamResponse:
    """Forward a request with a specific body to upstream."""
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
                response = web.StreamResponse(
                    status=upstream_resp.status,
                    headers=_strip_hop_by_hop(upstream_resp.headers),
                )
                # Echo X-Conversation-Id for client-side debugging.
                # (Added in the full flow; placeholder for now.)
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
