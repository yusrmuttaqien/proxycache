# llama_client.py

# -*- coding: utf-8 -*-

"""
HTTP client for llama.cpp: /v1/chat/completions (stream/non-stream),
/slots save/restore, /v1/models, /props, /slots, /models/sse.

- stream: build_request + send(stream=True), raw bytes.
- non-stream: strict JSON parse with a fallback if content-type is not JSON.
- /slots: filename + model in the JSON body (router mode requires model
  in the body; the ?model= query parameter is ignored).
- Slot pin is duplicated in body root / options / query.
- get_model_id(): fetches the current model id from /v1/models.
"""

import httpx
import logging
from typing import Dict, List, Optional, Tuple

from .config import REQUEST_TIMEOUT

log = logging.getLogger(__name__)


class LlamaClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT,
            limits=limits,
        )
        log.info("client_init url=%s httpx_version=%s", base_url, httpx.__version__)

    async def close(self):
        await self.client.aclose()

    @staticmethod
    def _with_slot_id(body: Dict, slot_id: Optional[int]) -> Tuple[Dict, Dict]:
        if slot_id is None:
            return body, {}

        new_body = dict(body)

        # root
        new_body["_slot_id"] = slot_id
        new_body["slot_id"] = slot_id
        new_body["id_slot"] = slot_id

        # options
        opts = dict(new_body.get("options") or {})
        opts["slot_id"] = slot_id
        opts["id_slot"] = slot_id
        new_body["options"] = opts

        # query
        query = {"slot_id": slot_id, "id_slot": slot_id}
        return new_body, query

    async def chat_completions(
        self,
        body: Dict,
        slot_id: Optional[int] = None,
        stream: bool = False,
    ):
        body2, query = self._with_slot_id(body, slot_id)

        if stream:
            req = self.client.build_request(
                "POST",
                "/v1/chat/completions",
                json=body2,
                params=query,
            )
            resp = await self.client.send(req, stream=True)
            return resp

        resp = await self.client.post(
            "/v1/chat/completions",
            json=body2,
            params=query,
        )
        resp.raise_for_status()

        ctype = resp.headers.get("content-type", "")
        if "application/json" not in ctype:
            raw = resp.text or ""
            log.error(
                "non_stream_non_json content_type=%s raw_len=%d",
                ctype,
                len(raw),
            )
            return {
                "object": "error",
                "message": "provider returned non-JSON",
                "raw": raw[:2048],
            }

        try:
            return resp.json()
        except Exception as e:
            raw = resp.text or ""
            log.error(
                "non_stream_json_parse_error status=%d raw_len=%d err=%s",
                resp.status_code,
                len(raw),
                e,
            )
            return {
                "object": "error",
                "message": "invalid json from provider",
                "raw": raw[:2048],
            }

    async def save_slot(
        self,
        slot_id: int,
        basename: str,
        model: Optional[str] = None,
    ) -> bool:
        # JSON body: {"filename": ..., "model": ...} — router mode requires
        # model in the body (query param ignored); without it the router 400s.
        payload: Dict = {"filename": basename}
        if model:
            payload["model"] = model
        try:
            resp = await self.client.post(
                f"/slots/{slot_id}",
                params={"action": "save"},
                json=payload,
            )
        except Exception as e:
            log.warning("save_slot_transport slot=%d model=%s: %s", slot_id, model, e)
            return False

        if resp.status_code != 200:
            log.warning(
                "save_slot_status=%d slot=%d model=%s basename=%s",
                resp.status_code,
                slot_id,
                model,
                basename[:16],
            )
            return False
        return True

    async def restore_slot(
        self,
        slot_id: int,
        basename: str,
        model: Optional[str] = None,
    ) -> bool:
        payload: Dict = {"filename": basename}
        if model:
            payload["model"] = model
        try:
            resp = await self.client.post(
                f"/slots/{slot_id}",
                params={"action": "restore"},
                json=payload,
            )
        except Exception as e:
            log.warning("restore_slot_transport slot=%d model=%s: %s", slot_id, model, e)
            return False

        if resp.status_code != 200:
            log.warning(
                "restore_slot_status=%d slot=%d model=%s basename=%s",
                resp.status_code,
                slot_id,
                model,
                basename[:16],
            )
            return False
        return True

    async def get_props(self, model: Optional[str] = None) -> Optional[Dict]:
        """GET /props (router: ?model=). Returns {is_sleeping, ...} or None on error."""
        try:
            params = {"model": model} if model else None
            resp = await self.client.get("/props", params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning("get_props_fail model=%s: %s", model, e)
            return None

    async def get_slots(self, model: Optional[str] = None) -> Optional[list]:
        """GET /slots (router: ?model=). Returns the slot state list or None."""
        try:
            params = {"model": model} if model else None
            resp = await self.client.get("/slots", params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else None
        except Exception as e:
            log.warning("get_slots_fail model=%s: %s", model, e)
            return None

    async def apply_template(self, body: Dict) -> Optional[str]:
        """POST /apply-template — the exact prompt text the model will see.

        Returns the rendered prompt, or None on any failure (caller falls
        back to raw_prefix hashing).
        """
        try:
            resp = await self.client.post("/apply-template", json=body)
            resp.raise_for_status()
            data = resp.json()
            # Verified: the field is "prompt", not "content".
            return data.get("prompt")
        except Exception as e:
            log.warning("apply_template_fail: %s", e)
            return None

    async def tokenize(self, text: str, model: Optional[str] = None) -> Optional[list]:
        """POST /tokenize — token ids for the given text, or None on failure.

        Router mode requires model in the body (400 without it).
        """
        payload = {"content": text}
        if model:
            payload["model"] = model
        try:
            resp = await self.client.post("/tokenize", json=payload)
            resp.raise_for_status()
            data = resp.json()
            tokens = data.get("tokens")
            return tokens if isinstance(tokens, list) else None
        except Exception as e:
            log.warning("tokenize_fail: %s", e)
            return None

    async def models_sse(self):
        """Raw SSE stream from /models/sse (router). Returns the streamed response."""
        req = self.client.build_request("GET", "/models/sse")
        resp = await self.client.send(req, stream=True)
        if resp.status_code != 200:
            log.warning("models_sse_status=%d", resp.status_code)
            await resp.aclose()
            return None
        return resp

    async def get_models(self) -> List[str]:
        """All model ids from /v1/models (empty on failure)."""
        try:
            resp = await self.client.get("/v1/models")
            resp.raise_for_status()
            data = resp.json()
            return [
                m["id"] for m in (data.get("data") or []) if isinstance(m, dict)
            ]
        except Exception as e:
            log.warning("get_models_failed: %s", e)
            return []

    async def get_slot_save_path(self) -> str:
        """The instance's --slot-save-path from GET /models (empty on failure).

        The model status carries the full command line (status.args), which
        includes --slot-save-path <dir> — the directory where .bin KV files
        live on the llama host.
        """
        try:
            resp = await self.client.get("/models")
            data = resp.json()
        except Exception as e:
            log.warning("save_path_fetch_fail: %s", e)
            return ""
        for m in data.get("data", []):
            args = (m.get("status") or {}).get("args") or []
            for i, a in enumerate(args):
                if a == "--slot-save-path" and i + 1 < len(args):
                    return args[i + 1]
        return ""

    async def get_model_id(self) -> str:
        """
        Fetch the first model id from /v1/models (empty string on failure).
        """
        models = await self.get_models()
        return models[0] if models else ""
