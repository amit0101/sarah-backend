"""OpenAI Responses API tool definitions — Section 8.1."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def sarah_tools(*, vector_store_id: Optional[str]) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = [
        {
            "type": "function",
            "name": "create_contact",
            # strict:false so phone/email can be omitted when not yet collected.
            # The backend find_or_create is idempotent — safe to call again with more fields.
            "description": (
                "Create or update the visitor's contact in CRM. "
                "Call as soon as you have first_name + last_name + at least phone OR email. "
                "If you collect more fields later (e.g. email after capturing phone) call again "
                "with all known values — the system updates, not duplicates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "phone": {
                        "type": "string",
                        "description": "E.164 or North American format — include if known",
                    },
                    "email": {
                        "type": "string",
                        "description": "Include if known",
                    },
                },
                "required": ["first_name", "last_name"],
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
                "Check funeral director availability for a specific date at the "
                "visitor's location. Returns which directors are on shift for that "
                "location's region (North or South) and the available appointment slots "
                "(typically 9:00 AM, 12:15 PM, 3:00 PM). Call this before offering "
                "appointment times to the visitor. The 'primary' field is internal "
                "routing context only — do NOT name the director to the visitor."
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
                "event with the family name, assigned funeral director, location, and type. "
                "Pass the 'primary' name returned by check_calendar as counselor_name "
                "(internal field name kept for backward compatibility; do NOT mention "
                "the director to the visitor)."
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
                        "description": "Assigned funeral director from check_calendar results (internal field name; do NOT name the director to the visitor)",
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
            "name": "reschedule_appointment",
            "strict": True,
            "description": (
                "Move an existing appointment to a new start/end time. Use the "
                "appointment_id returned by a previous book_appointment call. "
                "Updates the calendar event-of-record, any room hold, GHL, and "
                "the appointments table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "UUID of the appointment to move (returned by book_appointment).",
                    },
                    "start_iso": {
                        "type": "string",
                        "description": "New appointment start in ISO 8601 with timezone offset.",
                    },
                    "end_iso": {
                        "type": "string",
                        "description": "New appointment end (typically same duration as the original).",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional updated notes (replaces existing notes if provided).",
                    },
                },
                "required": ["appointment_id", "start_iso", "end_iso", "notes"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "cancel_appointment",
            "strict": True,
            "description": (
                "Cancel an existing appointment. Use the appointment_id returned by "
                "a previous book_appointment call. Removes the calendar event-of-record, "
                "any room hold, the GHL mirror, and marks the appointments row cancelled."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "UUID of the appointment to cancel (returned by book_appointment).",
                    },
                },
                "required": ["appointment_id"],
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
        # Phase B — proactive SMS continuation
        # See PROMPT_AND_TOOL_CHANGES_2026-04-18.md §"Phase B — Proactive continuation tool"
        {
            "type": "function",
            "name": "continue_on_sms",
            "strict": True,
            "description": (
                "Proactively continue this webchat conversation over SMS. Call ONLY after "
                "the visitor has explicitly agreed to continue by text AND a phone number "
                "is on file (via create_contact). Sends an opening text from the M&H number, "
                "flips the conversation channel to SMS so any later replies land in the same "
                "thread, and writes a handover row visible in the staff inbox. Idempotent — "
                "calling twice on the same conversation is a no-op. Do NOT call this for "
                "at-need conversations (escalate to staff instead) or before providing value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": (
                            "Visitor's phone in E.164 format (e.g. +14035551234). MUST match "
                            "the phone already captured on the contact via create_contact."
                        ),
                    },
                    "consent_text": {
                        "type": "string",
                        "description": (
                            "The exact CASL consent line the visitor agreed to in this turn. "
                            "Logged for compliance audit. Example: 'By sharing your number, "
                            "you're consenting to receive text messages from McInnis & Holloway "
                            "— standard rates may apply, reply STOP to opt out.'"
                        ),
                    },
                },
                "required": ["phone", "consent_text"],
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
