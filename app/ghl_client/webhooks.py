"""Parse and validate inbound GHL webhook payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class GHLInboundType(str, Enum):
    CAMPAIGN_REPLY = "campaign_reply"
    LIFECYCLE_REPLY = "lifecycle_reply"
    CALLBACK_REQUEST = "callback_request"


@dataclass
class CampaignReplyPayload:
    """Normalised campaign / lifecycle / callback webhook from GHL."""

    type: GHLInboundType
    contact_id: str
    message: str
    location_id: Optional[str]
    workflow_id: Optional[str]
    campaign_id: Optional[str]
    raw: Dict[str, Any]

    @property
    def is_callback_only(self) -> bool:
        return self.type == GHLInboundType.CALLBACK_REQUEST


def parse_campaign_reply_webhook(body: Dict[str, Any]) -> CampaignReplyPayload:
    """
    Accepts flexible GHL workflow payloads: may nest under 'contact', 'data', etc.
    """
    raw = dict(body)
    contact_id = (
        str(body.get("contact_id") or body.get("contactId") or "")
        or _dig(body, "contact", "id")
        or _dig(body, "data", "contactId")
        or ""
    )
    msg = (
        str(body.get("message") or body.get("body") or body.get("text") or "")
        or _dig(body, "message", "body")
        or ""
    )
    loc = body.get("location_id") or body.get("locationId") or _dig(body, "data", "locationId")
    if loc is not None:
        loc = str(loc)
    wf = body.get("workflow_id") or body.get("workflowId")
    camp = body.get("campaign_id") or body.get("campaignId")
    type_raw = str(
        body.get("type") or body.get("eventType") or GHLInboundType.CAMPAIGN_REPLY.value
    ).lower()
    if type_raw in ("lifecycle", "lifecycle_reply"):
        inbound_type = GHLInboundType.LIFECYCLE_REPLY
    elif type_raw in ("callback", "callback_request"):
        inbound_type = GHLInboundType.CALLBACK_REQUEST
    else:
        inbound_type = GHLInboundType.CAMPAIGN_REPLY
    return CampaignReplyPayload(
        type=inbound_type,
        contact_id=contact_id,
        message=msg,
        location_id=loc,
        workflow_id=str(wf) if wf else None,
        campaign_id=str(camp) if camp else None,
        raw=raw,
    )


def _dig(d: Dict[str, Any], *keys: str) -> Optional[str]:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return str(cur) if cur is not None else None


def parse_handoff_webhook(body: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """Returns (event_name, conversation_id) for comms handoff if present."""
    event = str(body.get("event") or "")
    conv = body.get("conversation_id") or body.get("conversationId")
    return event, str(conv) if conv else None
