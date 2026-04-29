"""Conversation path identifiers — Section 4.2."""

from enum import Enum


class ConversationPath(str, Enum):
    IMMEDIATE_NEED = "immediate_need"
    PRE_NEED = "pre_need"
    OBITUARY = "obituary"
    GENERAL = "general"
    # PET_CREMATION removed in session 18: pet inquiries are routed to staff
    # via escalate_to_staff from the GLOBAL_BRAND "Pet Inquiries" section.


PATH_LABELS: dict[str, str] = {
    ConversationPath.IMMEDIATE_NEED.value: "Immediate Need",
    ConversationPath.PRE_NEED.value: "Pre-Need Planning",
    ConversationPath.OBITUARY.value: "Obituary Lookup",
    ConversationPath.GENERAL.value: "General Question",
}
