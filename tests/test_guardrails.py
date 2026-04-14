"""Tests for guardrails — Section 4.7 pre-filter."""

import pytest

from app.conversation_engine.guardrails import evaluate_guardrails


class TestGuardrailsBlocking:
    """Test that extreme cases are blocked."""

    def test_violence_threat_blocked(self):
        result = evaluate_guardrails("I will kill everyone there")
        assert result.blocked is True
        assert result.reason == "violence_threat"
        assert "911" in result.reply

    def test_violence_threat_case_insensitive(self):
        result = evaluate_guardrails("I'M GOING TO KILL someone")
        assert result.blocked is True
        assert result.reason == "violence_threat"

    def test_suicide_bomb_blocked(self):
        result = evaluate_guardrails("suicide bomb at the building")
        assert result.blocked is True
        assert result.reason == "violence_threat"

    def test_spam_long_url_blocked(self):
        url = "https://spam.example.com/" + "x" * 100
        result = evaluate_guardrails(f"Check this out {url}")
        assert result.blocked is True
        assert result.reason == "spam"

    def test_spam_repeated_chars_blocked(self):
        # Pattern requires (.)\1{20,} — same character 21+ times in a row
        result = evaluate_guardrails("x" * 30)
        assert result.blocked is True
        assert result.reason == "spam"

    def test_spam_buy_now_blocked(self):
        result = evaluate_guardrails("buy now at https://spam.example.com")
        assert result.blocked is True
        assert result.reason == "spam"


class TestGuardrailsPassing:
    """Test that normal messages pass through."""

    def test_normal_message_passes(self):
        result = evaluate_guardrails("I'd like to learn about pre-planning options")
        assert result.blocked is False
        assert result.reply is None

    def test_emotional_message_passes(self):
        """Emotional sensitivity is handled by the AI, not the pre-filter."""
        result = evaluate_guardrails("My mother just passed away and I don't know what to do")
        assert result.blocked is False

    def test_profanity_passes(self):
        """Profanity is handled by the AI system prompt, not pre-filter."""
        result = evaluate_guardrails("This is such bullshit service")
        assert result.blocked is False

    def test_empty_message_passes(self):
        result = evaluate_guardrails("")
        assert result.blocked is False

    def test_whitespace_only_passes(self):
        result = evaluate_guardrails("   ")
        assert result.blocked is False

    def test_off_topic_passes(self):
        """Off-topic is handled by AI, not pre-filter."""
        result = evaluate_guardrails("What's the weather like today?")
        assert result.blocked is False

    def test_normal_url_passes(self):
        result = evaluate_guardrails("Check mcinnis.ca for details")
        assert result.blocked is False

    def test_grief_language_not_blocked(self):
        """Ensure grief-related language is never misidentified as violence."""
        result = evaluate_guardrails("I feel like I'm going to die of grief")
        assert result.blocked is False

    def test_short_repetition_passes(self):
        """Short repetition (under 20 chars) should pass."""
        result = evaluate_guardrails("hahahahaha")
        assert result.blocked is False
