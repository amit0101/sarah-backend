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
            "description": (
                "Check arrangement counselor availability for a specific date at the "
                "visitor's location. Returns which counselors are on shift for that "
                "location's region (North or South) and the available appointment slots "
                "(typically 9:00 AM, 12:15 PM, 3:00 PM). Call this before offering "
                "appointment times to the visitor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "Date to check, YYYY-MM-DD format",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA tz, default America/Edmonton",
                    },
                },
                "required": ["date", "timezone"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "book_appointment",
            "strict": True,
            "description": (
                "Book an arrangement appointment on the calendar. Creates a detailed "
                "event with the family name, assigned counselor, location, and type. "
                "Use a counselor name returned by check_calendar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_iso": {
                        "type": "string",
                        "description": "Appointment start in ISO 8601 with timezone offset",
                    },
                    "end_iso": {
                        "type": "string",
                        "description": "Appointment end (typically 90 min after start)",
                    },
                    "family_name": {
                        "type": "string",
                        "description": "Family's last name",
                    },
                    "counselor_name": {
                        "type": "string",
                        "description": "Assigned counselor from check_calendar results",
                    },
                    "appointment_type": {
                        "type": "string",
                        "enum": ["Arrangement", "Pre-Arrangement", "After Care"],
                        "description": "Type of appointment",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional details (need type, cultural affiliation, attendees)",
                    },
                },
                "required": [
                    "start_iso", "end_iso", "family_name",
                    "counselor_name", "appointment_type", "notes",
                ],
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
        {
            "type": "function",
            "name": "resolve_postal_code",
            "strict": True,
            "description": (
                "Resolve a Canadian postal code to the nearest McInnis & Holloway chapel. "
                "Call this when the visitor provides their postal code. Returns the nearest "
                "chapel name and slug, or an error if the postal code is invalid."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "postal_code": {
                        "type": "string",
                        "description": "Canadian postal code, e.g. T2P 1A1",
                    },
                },
                "required": ["postal_code"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "resolve_area",
            "strict": True,
            "description": (
                "Resolve a Calgary area to the nearest McInnis & Holloway chapel when "
                "postal code is unavailable or invalid. Use after the visitor chooses an area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "area": {
                        "type": "string",
                        "enum": ["south_calgary", "north_calgary", "airdrie", "cochrane"],
                        "description": "The area the visitor selected",
                    },
                },
                "required": ["area"],
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
