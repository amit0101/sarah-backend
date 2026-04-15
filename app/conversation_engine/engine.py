"""OpenAI Responses API loop with tools — Sections 4.1, 8.1."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI
from openai.types.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.conversation_engine.prompt_manager import build_system_prompt
from app.conversation_engine.tool_definitions import sarah_tools
from app.services.sarah_tools import SarahToolRunner, ToolContext

logger = logging.getLogger(__name__)


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
        self._client = AsyncOpenAI(api_key=s.openai_api_key) if s.openai_api_key else None
        self._model = s.openai_model

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

        for round_idx in range(max_rounds):
            if round_idx == 0:
                if chain_from_db:
                    kwargs: Dict[str, Any] = {
                        "model": self._model,
                        "previous_response_id": chain_from_db,
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
                    "input": current_input,
                    "tools": tools,
                }

            resp = await self._client.responses.create(**kwargs)
            last_resp = resp

            calls = [i for i in (resp.output or []) if i.type == "function_call"]
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
