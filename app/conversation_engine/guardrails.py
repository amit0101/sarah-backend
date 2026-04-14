"""Guardrails: minimal pre-filter for extreme cases — Section 4.7.

All nuanced guardrail behaviour (abuse detection, off-topic handling,
scope boundaries, emotional sensitivity) is handled by the AI model
through system prompt instructions in prompt_manager.py.

This module only catches extreme cases that should never reach the API:
- Explicit violence threats
- Obvious spam patterns
- Empty/whitespace messages

Per SARAH_BUILD_REVISION Section 2.4: "Use the AI model's built-in safety
and add a lightweight pre-check."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Section 4.7 — extreme-case pre-filter only
_VIOLENCE_PATTERN = re.compile(
    r"\b(i\s+will\s+kill|i'?m\s+going\s+to\s+(kill|hurt|harm)|"
    r"bomb\s+threat|shoot\s+up|going\s+to\s+die\s+by|suicide\s+bomb)\b",
    re.I,
)

_SPAM_PATTERN = re.compile(
    r"(https?://\S{80,})|"       # Extremely long URLs — group 1
    r"(.)\2{20,}|"                # Same character repeated 20+ times — group 2
    r"(buy\s+now\s+.*https?://)",  # Obvious spam
    re.I,
)


@dataclass
class GuardrailResult:
    blocked: bool
    reason: Optional[str]
    reply: Optional[str]


def evaluate_guardrails(user_text: str) -> GuardrailResult:
    """Minimal pre-filter — only blocks extreme cases before they reach OpenAI.

    All other guardrail behaviour (profanity, off-topic, scope, emotional
    sensitivity) is handled by GPT-4o through the system prompt. The model's
    built-in safety training is far more reliable than keyword matching.
    """
    text = user_text.strip()

    if not text:
        return GuardrailResult(False, None, None)

    # Explicit violence threats — these should not be sent to the API
    if _VIOLENCE_PATTERN.search(text):
        return GuardrailResult(
            True,
            "violence_threat",
            "I take all safety concerns seriously. If you or someone you know is in "
            "danger, please call 911 immediately. If you'd like to speak with a member "
            "of our team, I can connect you with someone right away.",
        )

    # Obvious spam — no point sending to the API
    if _SPAM_PATTERN.search(text):
        return GuardrailResult(
            True,
            "spam",
            "I'm here to help with questions about McInnis & Holloway's funeral services. "
            "How can I assist you today?",
        )

    # All other messages pass through to the AI model, which handles:
    # - Profanity → compassionate boundary (via system prompt)
    # - Off-topic → gentle redirect (via system prompt)
    # - Legal/medical/financial advice → decline + refer to staff (via system prompt)
    # - Emotional sensitivity → appropriate tone (via system prompt)
    return GuardrailResult(False, None, None)
