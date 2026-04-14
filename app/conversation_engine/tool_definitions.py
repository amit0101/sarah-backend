"""OpenAI Responses API tool definitions — Section 8.1."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def sarah_tools(*, vector_store_id: Optional[str]) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "name": "create_contact",
            "strict": True,
            "description": "Create or update the visitor's contact in CRM with name, phone, email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "phone": {"type": "string", "description": "E.164 or North American format"},
                    "email": {"type": "string"},
                },
                # Strict mode: every property key must appear in required; use "" when unknown.
                "required": ["first_name", "last_name", "phone", "email"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "apply_tag",
            "strict": True,
            "description": "Apply a CRM tag by logical key (e.g. entry_webchat, hot_lead). Mapped in location config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_key": {"type": "string"},
                },
                "required": ["tag_key"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "move_pipeline",
            "strict": True,
            "description": "Move contact in a sales pipeline using logical keys from location config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_key": {"type": "string", "description": "e.g. pre_need or at_need"},
                    "stage_key": {"type": "string", "description": "e.g. new_lead, contacted, appointment_set"},
                },
                "required": ["pipeline_key", "stage_key"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "check_calendar",
            "strict": True,
            "description": "Check appointment availability for this location in a time window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_iso": {"type": "string"},
                    "end_iso": {"type": "string"},
                    "timezone": {"type": "string", "description": "IANA tz, e.g. America/Edmonton"},
                },
                "required": ["start_iso", "end_iso", "timezone"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "book_appointment",
            "strict": True,
            "description": "Book an appointment on Google Calendar and sync to GHL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_iso": {"type": "string"},
                    "end_iso": {"type": "string"},
                    "title": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["start_iso", "end_iso", "title", "notes"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "search_obituary",
            "strict": True,
            "description": "Search obituaries via Tribute Center.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "date": {"type": "string"},
                    "location_hint": {"type": "string"},
                },
                "required": ["name", "date", "location_hint"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "escalate_to_staff",
            "strict": True,
            "description": "Escalate to human staff at this location; sends notifications.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["normal", "high"]},
                },
                "required": ["reason", "urgency"],
                "additionalProperties": False,
            },
        },
        # Section 2.5 / 4.2 — AI-driven topic switching replaces keyword detection
        {
            "type": "function",
            "name": "switch_conversation_path",
            "strict": True,
            "description": (
                "Call this when the user has clearly changed the topic of conversation "
                "to a different service area. For example, they started asking about "
                "preplanning but now reveal an immediate need."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "new_path": {
                        "type": "string",
                        "enum": [
                            "immediate_need",
                            "pre_need",
                            "obituary",
                            "general",
                            "pet_cremation",
                        ],
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for the path switch",
                    },
                },
                "required": ["new_path", "reason"],
                "additionalProperties": False,
            },
        },
    ]
    if vector_store_id:
        tools.insert(
            0,
            {
                "type": "file_search",
                "vector_store_ids": [vector_store_id],
            },
        )
    return tools
