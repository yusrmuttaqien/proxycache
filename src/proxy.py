"""Transparent forward — proxy every request to upstream, byte-identical.

Spec (Transparency section): all requests proxied, responses streamed back
byte-identical. This is the Phase 0 item 5 serve loop.
"""

from __future__ import annotations

import aiohttp
from aiohttp import web

# Hop-by-hop headers (RFC 7230) that must not be forwarded.
HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})


def _strip_hop_by_hop(headers) -> dict:
    """Drop hop-by-hop headers; keep the rest."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


async def proxy_handler(request: web.Request, upstream: str) -> web.StreamResponse:
    """Forward one request to upstream and stream the response back."""
    url = f"http://{upstream}{request.raw_path}"
    headers = _strip_hop_by_hop(request.headers)
    headers.pop("Host", None)  # aiohttp sets Host from the URL
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


def make_app(upstream: str) -> web.Application:
    """Build the proxy app with a catch-all route forwarding to upstream."""
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", lambda r: proxy_handler(r, upstream))
    return app
