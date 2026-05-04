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
    """Pre-need and at-need pipeline IDs and stage IDs.

    Supports two schemas in `sarah.locations.config`:

    - Current (live MHFH locations) — flat per-pipeline keys::

        {
          "ghl_pipelines": {
            "pre_need": {"pipeline_id": "...", "stage_new_lead": "..."},
            "at_need":  {"pipeline_id": "...", "stage_new": "..."}
          }
        }

    - Legacy (test fixtures, older docs) — nested ``stages`` map::

        {
          "pipeline_map": {
            "pre_need": {"pipeline_id": "...", "stages": {"new_lead": "..."}},
            "at_need":  {"pipeline_id": "...", "stages": {"new": "..."}}
          }
        }
    """
    if not config:
        return {}
    return dict(config.get("ghl_pipelines") or config.get("pipeline_map") or {})


def get_pipeline_stage_id(pipe_cfg: Optional[Dict[str, Any]], stage_key: str) -> Optional[str]:
    """Resolve a stage id from a per-pipeline config block.

    Tolerant of both the flat ``stage_<key>`` schema and the legacy
    ``stages: {<key>: ...}`` nested schema. See `get_pipeline_map` for
    the full schema description.
    """
    if not pipe_cfg or not stage_key:
        return None
    flat = pipe_cfg.get(f"stage_{stage_key}")
    if flat:
        return str(flat)
    nested = (pipe_cfg.get("stages") or {}).get(stage_key)
    return str(nested) if nested else None


def resolve_tag_key(config: Optional[Dict[str, Any]], key: str) -> Optional[str]:
    m = get_tag_map(config)
    return m.get(key)
