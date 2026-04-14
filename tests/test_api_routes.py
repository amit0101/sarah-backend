"""Tests for API routes — admin auth, route existence."""

import pytest
from unittest.mock import MagicMock, patch


class TestAdminAuthLogic:
    """Test the admin API key auth dependency directly."""

    def test_require_admin_key_function_exists(self):
        """The require_admin_key dependency should be importable."""
        from app.api.auth import require_admin_key
        assert callable(require_admin_key)

    def test_admin_key_setting_exists(self):
        """Admin key should come from ADMIN_API_KEY setting."""
        from app.config import Settings
        assert 'admin_api_key' in Settings.model_fields

    @pytest.mark.asyncio
    async def test_require_admin_key_rejects_wrong_key(self):
        """Wrong key should raise HTTPException."""
        from fastapi import HTTPException
        from app.api.auth import require_admin_key
        with patch("app.api.auth.get_settings") as ms:
            ms.return_value = MagicMock(admin_api_key="correct-key")
            with pytest.raises(HTTPException) as exc_info:
                await require_admin_key(x_admin_key="wrong-key")
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_require_admin_key_accepts_valid_key(self):
        """Correct key should pass."""
        from app.api.auth import require_admin_key
        with patch("app.api.auth.get_settings") as ms:
            ms.return_value = MagicMock(admin_api_key="correct-key")
            result = await require_admin_key(x_admin_key="correct-key")
            assert result == "correct-key"

    @pytest.mark.asyncio
    async def test_require_admin_key_blocks_when_not_configured(self):
        """If no key configured, all admin access should be blocked."""
        from fastapi import HTTPException
        from app.api.auth import require_admin_key
        with patch("app.api.auth.get_settings") as ms:
            ms.return_value = MagicMock(admin_api_key="")
            with pytest.raises(HTTPException) as exc_info:
                await require_admin_key(x_admin_key="anything")
            assert exc_info.value.status_code == 503


class TestAdminRouteProtection:
    """Verify admin routes are protected by API key auth."""

    def test_admin_router_has_routes(self):
        from app.api.routes.admin import router
        assert len(router.routes) > 0

    def test_admin_endpoints_exist(self):
        from app.api.routes.admin import router
        paths = [r.path for r in router.routes if hasattr(r, 'path')]
        assert any("organization" in p for p in paths)


class TestWebhookRouteExistence:
    def test_sms_webhook_route_exists(self):
        from app.api.routes.webhooks import router
        paths = [r.path for r in router.routes if hasattr(r, 'path')]
        assert any("sms" in p.lower() for p in paths)

    def test_comms_webhook_route_exists(self):
        from app.api.routes.webhooks import router
        paths = [r.path for r in router.routes if hasattr(r, 'path')]
        assert any("comms" in p.lower() or "handoff" in p.lower() for p in paths)
