"""Tests for webhook dispatcher — retry logic, event emission."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.webhooks.dispatcher import WebhookDispatcher


@pytest.fixture
def dispatcher():
    with patch("app.webhooks.dispatcher.get_settings") as mock_settings:
        s = MagicMock()
        s.comms_platform_webhook_url = "https://comms.test/webhook"
        s.comms_webhook_secret = ""
        s.sarah_version = "1.0"
        mock_settings.return_value = s
        d = WebhookDispatcher()
        return d


class TestWebhookEmission:
    @pytest.mark.asyncio
    async def test_emit_creates_background_task(self, dispatcher):
        """Verify emit creates a background task (fire-and-forget)."""
        with patch("app.webhooks.dispatcher.asyncio.create_task") as mock_task:
            with patch("app.webhooks.dispatcher.get_settings") as ms:
                ms.return_value = MagicMock(sarah_version="1.0")
                await dispatcher.emit("conversation.started", {"conversation_id": "123"})
                mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_skips_when_no_url(self):
        """If no webhook URL configured, emit should be a no-op."""
        with patch("app.webhooks.dispatcher.get_settings") as mock_settings:
            s = MagicMock()
            s.comms_platform_webhook_url = ""
            s.comms_webhook_secret = ""
            mock_settings.return_value = s
            d = WebhookDispatcher()
            # Should not raise and should not create tasks
            with patch("app.webhooks.dispatcher.asyncio.create_task") as mock_task:
                await d.emit("test.event", {"data": "value"})
                mock_task.assert_not_called()


class TestWebhookDispatcherInit:
    def test_stores_url(self):
        with patch("app.webhooks.dispatcher.get_settings") as mock_settings:
            s = MagicMock()
            s.comms_platform_webhook_url = "https://example.com/hook"
            s.comms_webhook_secret = "secret123"
            mock_settings.return_value = s
            d = WebhookDispatcher()
            assert d._url == "https://example.com/hook"
            assert d._secret == "secret123"

    def test_empty_url(self):
        with patch("app.webhooks.dispatcher.get_settings") as mock_settings:
            s = MagicMock()
            s.comms_platform_webhook_url = ""
            s.comms_webhook_secret = ""
            mock_settings.return_value = s
            d = WebhookDispatcher()
            assert d._url == ""
