"""Conversation path identifiers — Section 4.2."""

from enum import Enum


class ConversationPath(str, Enum):
    IMMEDIATE_NEED = "immediate_need"
    PRE_NEED = "pre_need"
    OBITUARY = "obituary"
    GENERAL = "general"
    PET_CREMATION = "pet_cremation"


PATH_LABELS: dict[str, str] = {
    ConversationPath.IMMEDIATE_NEED.value: "Immediate Need",
    ConversationPath.PRE_NEED.value: "Pre-Need Planning",
    ConversationPath.OBITUARY.value: "Obituary Lookup",
    ConversationPath.GENERAL.value: "General Question",
    ConversationPath.PET_CREMATION.value: "Pet Cremation",
}
