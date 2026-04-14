"""Pydantic request/response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ChatMessageIn(BaseModel):
    conversation_id: Optional[uuid.UUID] = None
    organization_slug: str = Field(..., description="Organization slug (tenant)")
    location_id: str = Field(..., description="Location slug within the organization")
    message: str


class ChatMessageOut(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    responded: bool = True


class MessageRow(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    channel: str
    created_at: datetime

    model_config = {"from_attributes": True}


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1)
    slug: str = Field(..., min_length=1)
    status: str = "active"
    ghl_api_key: str = Field(..., min_length=1)
    ghl_location_id: str = Field(..., min_length=1)
    twilio_phone_number: Optional[str] = None

    @field_validator("name", "slug", "ghl_api_key", "ghl_location_id", mode="before")
    @classmethod
    def strip_required(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    status: Optional[str] = None
    ghl_api_key: Optional[str] = None
    ghl_location_id: Optional[str] = None
    twilio_phone_number: Optional[str] = None


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    ghl_location_id: Optional[str] = None
    vector_store_id: Optional[str] = None
    calendar_id: Optional[str] = None
    ghl_calendar_id: Optional[str] = None
    availability_calendar_id: Optional[str] = None
    escalation_contacts: Optional[list] = None
    config: Optional[dict[str, Any]] = None


class LocationCreate(BaseModel):
    """id = location slug within the org (e.g. main_office); must be non-empty."""

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    ghl_location_id: Optional[str] = None
    vector_store_id: Optional[str] = None
    calendar_id: Optional[str] = None
    ghl_calendar_id: Optional[str] = None
    availability_calendar_id: Optional[str] = None
    escalation_contacts: Optional[list] = None
    config: Optional[dict[str, Any]] = None

    @field_validator("id", "name", mode="before")
    @classmethod
    def strip_location_id_name(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v


class PromptUpdate(BaseModel):
    global_instructions: Optional[str] = None
    path_instructions: Optional[str] = None
    extra_config: Optional[dict[str, Any]] = None


class HandoffWebhook(BaseModel):
    event: str
    conversation_id: uuid.UUID
    staff_id: Optional[str] = None
    staff_name: Optional[str] = None
    timestamp: Optional[str] = None
