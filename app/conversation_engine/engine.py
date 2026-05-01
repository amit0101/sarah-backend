"""OpenAI Responses API loop with tools — Sections 4.1, 8.1."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI
from openai.types.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.conversation_engine.prompt_manager import build_system_prompt
from app.conversation_engine.tool_definitions import sarah_tools
from app.models.openai_response_log import OpenAIResponseLog
from app.services.sarah_tools import SarahToolRunner, ToolContext

logger = logging.getLogger(__name__)


# Session 21 — reuse a single AsyncOpenAI across requests so httpx's connection
# pool (TLS handshake, HTTP/2 session) is preserved between turns. Previously a
# fresh client was instantiated per ConversationEngine; the local bench in
# session 20 that measured +745 ms SDK overhead vs raw httpx was done with a
# REUSED client, so prod (one-shot clients) was paying more than that.
_shared_client: Optional[AsyncOpenAI] = None


def _get_openai_client() -> Optional[AsyncOpenAI]:
    """Return a process-wide AsyncOpenAI, or None when no key is configured.
    Checking settings on every call keeps tests that patch `get_settings`
    with an empty key working correctly."""
    global _shared_client
    s = get_settings()
    if not s.openai_api_key:
        return None
    if _shared_client is None:
        _shared_client = AsyncOpenAI(api_key=s.openai_api_key)
    return _shared_client


async def warmup_openai_client() -> None:
    """Called at FastAPI startup to pay the TLS-handshake cost once,
    not on the first user message after each Render cold start.
    Safe to call without credentials (no-op) and safe on failure (logged)."""
    client = _get_openai_client()
    if client is None:
        logger.info("openai_warmup: skipped (no OPENAI_API_KEY)")
        return
    try:
        t0 = time.monotonic()
        await client.models.list()
        logger.info("openai_warmup: ok (%d ms)", int((time.monotonic() - t0) * 1000))
    except Exception as e:
        logger.warning("openai_warmup: failed (%s); first request will pay handshake cost", e)


def _serialize_response(resp: Response) -> Dict[str, Any]:
    try:
        if hasattr(resp, "model_dump"):
            return resp.model_dump(mode="json")  # type: ignore[no-any-return]
    except Exception:
        logger.debug("model_dump failed for response", exc_info=True)
    return {"id": getattr(resp, "id", None), "error": "serialization_failed"}


def _extract_text(resp: Response) -> str:
    parts: List[str] = []
    for item in resp.output or []:
        if item.type == "message":
            for c in item.content:
                if getattr(c, "type", None) == "output_text":
                    parts.append(c.text)
    return "\n".join(parts).strip()


class ConversationEngine:
    def __init__(self, db: AsyncSession, tool_ctx: ToolContext) -> None:
        self._db = db
        self._ctx = tool_ctx
        self._runner = SarahToolRunner()
        s = get_settings()
        # Tests may override self._client with a MagicMock post-construction;
        # preserve that contract while sharing the client by default.
        self._client = _get_openai_client()
        self._model = s.openai_model

    async def _persist_openai_response(
        self,
        resp: Response,
        round_idx: int,
        *,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        tid = getattr(self._ctx, "turn_id", None)
        if not isinstance(tid, uuid.UUID):
            return
        try:
            payload = _serialize_response(resp)
            if meta:
                payload["_meta"] = meta
            self._db.add(
                OpenAIResponseLog(
                    conversation_id=self._ctx.conversation.id,
                    turn_id=tid,
                    round_index=round_idx,
                    openai_response_id=resp.id,
                    payload=payload,
                )
            )
            await self._db.flush()
        except Exception:
            logger.exception("Failed to persist OpenAI response log (turn_id=%s)", tid)

    async def run_turn(
        self,
        *,
        user_text: str,
        previous_response_id: Optional[str],
        path: str,
        instructions_override: Optional[str] = None,
    ) -> Tuple[str, str]:
        if not self._client:
            raise RuntimeError("OPENAI_API_KEY not configured")

        loc = self._ctx.location
        instructions = instructions_override or await build_system_prompt(
            self._db,
            location=loc,
            path=path,
        )
        org = self._ctx.organization
        vs = org.vector_store_id if org else None
        tools = sarah_tools(vector_store_id=vs)

        chain_from_db = previous_response_id
        last_resp: Optional[Response] = None
        max_rounds = 12
        current_input: Any = None

        # OpenAI Responses API: when using previous_response_id, system instructions from
        # earlier turns are NOT retained — you must pass `instructions` on every request
        # or the model loses tool guidance and behavioural rules (GLOBAL_BRAND).
        for round_idx in range(max_rounds):
            if round_idx == 0:
                if chain_from_db:
                    kwargs: Dict[str, Any] = {
                        "model": self._model,
                        "previous_response_id": chain_from_db,
                        "instructions": instructions,
                        "input": user_text,
                        "tools": tools,
                    }
                else:
                    kwargs = {
                        "model": self._model,
                        "instructions": instructions,
                        "input": user_text,
                        "tools": tools,
                    }
            else:
                assert last_resp is not None
                kwargs = {
                    "model": self._model,
                    "previous_response_id": last_resp.id,
                    "instructions": instructions,
                    "input": current_input,
                    "tools": tools,
                }

            t0 = time.monotonic()
            resp = await self._client.responses.create(**kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            last_resp = resp

            calls = [i for i in (resp.output or []) if i.type == "function_call"]
            usage_dump: Optional[Dict[str, Any]] = None
            usage_obj = getattr(resp, "usage", None)
            if usage_obj is not None and hasattr(usage_obj, "model_dump"):
                try:
                    usage_dump = usage_obj.model_dump(mode="json")
                except Exception:
                    usage_dump = None
            has_file_search = any(
                isinstance(t, dict) and t.get("type") == "file_search" for t in tools
            )
            meta = {
                "model": self._model,
                "round": round_idx,
                "duration_ms": duration_ms,
                "tools_count": len(tools),
                "has_file_search": has_file_search,
                "path": path,
                "num_function_calls": len(calls),
                "usage": usage_dump,
            }
            # Greppable structured log line for latency analysis (session 20).
            logger.info("openai_call %s", json.dumps(meta, default=str))
            await self._persist_openai_response(resp, round_idx, meta=meta)

            if not calls:
                text = _extract_text(resp)
                return text, resp.id

            outputs: List[Dict] = []
            for call in calls:
                if call.type != "function_call":
                    continue
                out = await self._runner.run(call.name, call.arguments, self._ctx)
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": out,
                    }
                )
            current_input = outputs

        if last_resp:
            return _extract_text(last_resp), last_resp.id
        return "", ""
