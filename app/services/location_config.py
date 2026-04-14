"""Load tag/pipeline mappings from sarah.locations.config (JSON)."""

from __future__ import annotations

from typing import Any, Dict, Optional


def get_tag_map(config: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Maps logical keys (e.g. entry_webchat) to GHL tag names."""
    if not config:
        return {}
    m = config.get("tag_map") or config.get("tags") or {}
    return {str(k): str(v) for k, v in m.items()}


def get_pipeline_map(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pre-need and at-need pipeline IDs and stage IDs."""
    if not config:
        return {}
    return dict(config.get("pipeline_map") or {})


def resolve_tag_key(config: Optional[Dict[str, Any]], key: str) -> Optional[str]:
    m = get_tag_map(config)
    return m.get(key)
