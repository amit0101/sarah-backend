"""AI-powered intent classification → conversation path — Section 4.2.

Replaces keyword matching with structured output classification via OpenAI.
Classification runs on first message only (when active_path is None).
Subsequent path changes handled by the switch_conversation_path tool.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from openai import AsyncOpenAI

from app.config import get_settings
from app.conversation_engine.paths import ConversationPath

logger = logging.getLogger(__name__)

# Section 4.2 — path definitions for the classification prompt
_CLASSIFICATION_PROMPT = """You are an intent classifier for a funeral home's customer service chatbot.

Given a customer's message, classify their intent into exactly one of these conversation paths:

- immediate_need — A family member has just died or passed away, the family needs urgent help or services now. Keywords: death, passed away, just died, urgent, deceased, at-need, body.
- pre_need — The person is interested in preplanning or pre-arranging funeral services for the future. Keywords: preplan, advance planning, plan ahead, future arrangements, consultation.
- obituary — The person is looking up an obituary, memorial details, or service times for someone who passed. Keywords: obituary, memorial, service times, tribute, funeral notice.
- general — General questions about services, pricing, locations, hours, pet cremation inquiries, or anything that doesn't fit the other categories. This is the default. (Pet inquiries land here and are routed to staff per the Pet Inquiries section in the system prompt.)

Important context: This is a funeral home. Messages may contain grief-related language. Classify based on the person's INTENT, not just the presence of certain words.

Respond with JSON only: {"path": "<path_value>", "confidence": <0.0-1.0>}"""


async def classify_path(
    user_text: str,
    previous_path: Optional[str] = None,
) -> str:
    """Classify user intent into a conversation path using AI structured output.

    Only called when active_path is None (first message of conversation).
    For subsequent messages, the cached path is used and topic switching
    is handled by the switch_conversation_path tool (Section 2.5).
    """
    # If path is already set, return it — no reclassification needed
    if previous_path:
        return previous_path

    text = user_text.strip()
    if not text:
        return ConversationPath.GENERAL.value

    settings = get_settings()
    if not settings.openai_api_key:
        logger.warning("No OpenAI API key configured; defaulting to GENERAL path")
        return ConversationPath.GENERAL.value

    try:
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        resp = await client.responses.create(
            model=settings.openai_model,
            instructions=_CLASSIFICATION_PROMPT,
            input=text,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "path_classification",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "enum": [p.value for p in ConversationPath],
                            },
                            "confidence": {"type": "number"},
                        },
                        "required": ["path", "confidence"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                },
            },
        )
        # Extract text from response
        result_text = ""
        for item in resp.output or []:
            if item.type == "message":
                for c in item.content:
                    if getattr(c, "type", None) == "output_text":
                        result_text = c.text
                        break

        if result_text:
            parsed = json.loads(result_text)
            path = parsed.get("path", ConversationPath.GENERAL.value)
            confidence = parsed.get("confidence", 0.0)
            logger.info(
                "Path classified: %s (confidence: %.2f) for text: %s",
                path,
                confidence,
                text[:80],
            )
            # Validate path is a known value
            valid_paths = {p.value for p in ConversationPath}
            if path in valid_paths:
                return path
            logger.warning("Unknown path '%s' from classifier; defaulting to GENERAL", path)

    except Exception:
        logger.exception("Path classification failed; defaulting to GENERAL")

    return ConversationPath.GENERAL.value
