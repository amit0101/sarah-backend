"""Tests for conversation engine — OpenAI Responses API loop."""

import json
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from app.conversation_engine.engine import ConversationEngine, _extract_text


class TestExtractText:
    def test_extracts_output_text(self):
        resp = MagicMock()
        resp.output = [
            MagicMock(
                type="message",
                content=[MagicMock(type="output_text", text="Hello there!")],
            )
        ]
        assert _extract_text(resp) == "Hello there!"

    def test_extracts_multiple_parts(self):
        resp = MagicMock()
        resp.output = [
            MagicMock(
                type="message",
                content=[
                    MagicMock(type="output_text", text="Part 1"),
                    MagicMock(type="output_text", text="Part 2"),
                ],
            )
        ]
        assert _extract_text(resp) == "Part 1\nPart 2"

    def test_empty_output(self):
        resp = MagicMock()
        resp.output = []
        assert _extract_text(resp) == ""

    def test_none_output(self):
        resp = MagicMock()
        resp.output = None
        assert _extract_text(resp) == ""

    def test_ignores_non_message_items(self):
        resp = MagicMock()
        resp.output = [
            MagicMock(type="function_call", content=[]),
            MagicMock(
                type="message",
                content=[MagicMock(type="output_text", text="Only this")],
            ),
        ]
        assert _extract_text(resp) == "Only this"


def _make_mock_location():
    loc = MagicMock()
    loc.id = "main_office"
    loc.organization_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    loc.vector_store_id = "vs_test123"
    loc.config = {}
    return loc


def _make_mock_ctx():
    """Build a mock ToolContext without needing real DB."""
    ctx = MagicMock()
    ctx.location = _make_mock_location()
    ctx.db = MagicMock()
    return ctx


class TestConversationEngine:
    @pytest.mark.asyncio
    async def test_run_turn_returns_text_and_response_id(self):
        """Engine should return AI text and response ID on a simple turn."""
        ctx = _make_mock_ctx()

        with patch("app.conversation_engine.engine.get_settings") as mock_s:
            mock_s.return_value = MagicMock(openai_api_key="test", openai_model="gpt-4o")
            engine = ConversationEngine(MagicMock(), ctx)

        # Mock the OpenAI client
        mock_resp = MagicMock()
        mock_resp.id = "resp_abc123"
        mock_resp.output = [
            MagicMock(
                type="message",
                content=[MagicMock(type="output_text", text="I'm Sarah, how can I help?")],
            )
        ]
        engine._client = MagicMock()
        engine._client.responses.create = AsyncMock(return_value=mock_resp)

        with patch("app.conversation_engine.engine.build_system_prompt", new_callable=AsyncMock) as mock_prompt:
            mock_prompt.return_value = "You are Sarah..."
            text, rid = await engine.run_turn(
                user_text="Hello",
                previous_response_id=None,
                path="general",
            )
        assert text == "I'm Sarah, how can I help?"
        assert rid == "resp_abc123"

    @pytest.mark.asyncio
    async def test_run_turn_handles_tool_calls(self):
        """Engine should execute tools and loop back for final text."""
        ctx = _make_mock_ctx()

        with patch("app.conversation_engine.engine.get_settings") as mock_s:
            mock_s.return_value = MagicMock(openai_api_key="test", openai_model="gpt-4o")
            engine = ConversationEngine(MagicMock(), ctx)

        # Round 1: function_call output
        tool_resp = MagicMock()
        tool_resp.id = "resp_tool1"
        tool_call = MagicMock()
        tool_call.type = "function_call"
        tool_call.name = "switch_conversation_path"
        tool_call.arguments = json.dumps({"path": "pre_need"})
        tool_call.call_id = "call_123"
        tool_resp.output = [tool_call]

        # Round 2: message output
        final_resp = MagicMock()
        final_resp.id = "resp_final"
        final_resp.output = [
            MagicMock(
                type="message",
                content=[MagicMock(type="output_text", text="Let me help with pre-planning.")],
            )
        ]

        engine._client = MagicMock()
        engine._client.responses.create = AsyncMock(side_effect=[tool_resp, final_resp])

        # Mock the tool runner
        engine._runner = MagicMock()
        engine._runner.run = AsyncMock(return_value='{"ok": true}')

        with patch("app.conversation_engine.engine.build_system_prompt", new_callable=AsyncMock) as mock_prompt:
            mock_prompt.return_value = "You are Sarah..."
            text, rid = await engine.run_turn(
                user_text="I want to plan ahead",
                previous_response_id=None,
                path="general",
            )
        assert text == "Let me help with pre-planning."
        assert rid == "resp_final"
        assert engine._client.responses.create.call_count == 2

    @pytest.mark.asyncio
    async def test_run_turn_raises_without_api_key(self):
        ctx = _make_mock_ctx()

        with patch("app.conversation_engine.engine.get_settings") as mock_s:
            mock_s.return_value = MagicMock(openai_api_key="", openai_model="gpt-4o")
            engine = ConversationEngine(MagicMock(), ctx)

        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            await engine.run_turn(user_text="Hello", previous_response_id=None, path="general")

    @pytest.mark.asyncio
    async def test_run_turn_chains_with_previous_response_id(self):
        """When previous_response_id is set, engine should chain conversation."""
        ctx = _make_mock_ctx()

        with patch("app.conversation_engine.engine.get_settings") as mock_s:
            mock_s.return_value = MagicMock(openai_api_key="test", openai_model="gpt-4o")
            engine = ConversationEngine(MagicMock(), ctx)

        mock_resp = MagicMock()
        mock_resp.id = "resp_chain"
        mock_resp.output = [
            MagicMock(type="message", content=[MagicMock(type="output_text", text="Follow-up reply")])
        ]
        engine._client = MagicMock()
        engine._client.responses.create = AsyncMock(return_value=mock_resp)

        with patch("app.conversation_engine.engine.build_system_prompt", new_callable=AsyncMock) as mock_prompt:
            mock_prompt.return_value = "You are Sarah..."
            text, rid = await engine.run_turn(
                user_text="Follow-up question",
                previous_response_id="resp_prev123",
                path="pre_need",
            )
        assert text == "Follow-up reply"
        # Verify previous_response_id was passed
        call_kwargs = engine._client.responses.create.call_args.kwargs
        assert call_kwargs["previous_response_id"] == "resp_prev123"
