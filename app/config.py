"""Application configuration from environment variables."""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings — all secrets from environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "Sarah API"
    debug: bool = False
    sarah_version: str = "1.0"
    log_level: str = "INFO"

    # Database (Supabase Postgres — use pooler URL in production)
    database_url: str = Field(
        ...,
        description="Async PostgreSQL URL, e.g. postgresql+asyncpg://...",
    )
    # SQLAlchemy QueuePool: cap concurrent connections per process. Supabase Session pooler
    # enforces a low global client limit; default 5+10 overflow can exhaust it alongside Comms.
    # Override via DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_TIMEOUT if needed.
    db_pool_size: int = Field(default=3, ge=1, le=50)
    db_max_overflow: int = Field(default=2, ge=0, le=50)
    db_pool_timeout: int = Field(default=30, ge=5, le=300)

    # Supabase (REST / optional client usage)
    supabase_url: str = ""
    supabase_key: str = ""

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # GHL API V2 — optional bootstrap / first-org fallback (per-org credentials in DB)
    ghl_api_key: str = ""
    ghl_location_id: str = ""
    ghl_api_base_url: str = "https://services.leadconnectorhq.com"
    ghl_api_version: str = "2021-07-28"

    # GHL calendar IDs for Sarah-originated appointments. Today every
    # location is mirrored onto the same two MHFH event calendars regardless
    # of which physical chapel the booking is for; the per-location
    # `sarah.locations.ghl_calendar_id` column is still consulted first so a
    # future per-location split (one GHL calendar per chapel) drops in by
    # populating that column. Until then, these env defaults win.
    #
    # Preplanning Calendar (pre-need flow) and Immediate Need Calendar
    # (at-need flow) — defaults match MHFH GHL location S703WHSXhCWXaI0K86Cz.
    ghl_calendar_id_preneed: str = "FV8WnbuvXxwCQH80ORsA"
    ghl_calendar_id_atneed: str = "teaR5VhrF6SYinfEgj0w"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    # Comma-separated list of E.164 numbers Sarah will silently ignore on inbound
    # SMS. Used to silence dev-test misfires (see SESSION_HANDOFF.md session 13
    # "Operational lesson — DO NOT REPEAT"). Inbound matching numbers return
    # 200 + empty TwiML; no contact, conversation, or AI reply is created.
    sms_inbound_blocklist: str = ""

    # B-soft.1 — outbound SMS rate limit guard (session 16). When enabled,
    # SmsService.send() refuses to deliver more than `sms_rate_limit_per_24h`
    # messages to any single E.164 destination within a rolling 24h window.
    # In-memory sliding-window counter (per-process; Sarah backend is single
    # instance on Render so this is sufficient). On limit hit: WARNING log +
    # returns None (same shape as Twilio-not-configured). Default: disabled
    # for backward compat. Set `SMS_RATE_LIMIT_ENABLED=true` on Render to
    # turn on; tune `SMS_RATE_LIMIT_PER_24H` per ops policy.
    sms_rate_limit_enabled: bool = False
    sms_rate_limit_per_24h: int = Field(default=20, ge=1, le=10000)

    # B-soft.2 — Twilio Lookup pre-flight (session 16). When enabled,
    # SmsService.send() calls Twilio Lookup v2 (line_type_intelligence) before
    # the actual messages.create() and rejects destinations whose carrier type
    # is not in `sms_lookup_allowed_types` (CSV of: mobile, landline, voip,
    # fixedVoip, nonFixedVoip, personal, tollFree, sharedCost, uan, voicemail,
    # pager, unknown). Default allowed: mobile only. Lookups are ~$0.005 each
    # and cached in-process for `sms_lookup_cache_ttl_seconds` to amortize.
    # On rejection: WARNING log + returns None. On Lookup API failure: fail
    # OPEN (proceed with send) and log — Twilio Lookup outages must not block
    # the SMS path. Default: disabled for backward compat.
    sms_lookup_enabled: bool = False
    sms_lookup_allowed_types: str = "mobile"
    sms_lookup_cache_ttl_seconds: int = Field(default=86400, ge=60, le=2592000)

    # Google Calendar — path to service account JSON or OAuth credentials file
    google_calendar_credentials: str = ""
    google_calendar_delegation_email: Optional[str] = None

    # Tribute Center / obituaries — Tribute Center Online (TCO) is the platform
    # behind www.mhfh.com/obituaries. Read endpoints are anonymous: only the
    # `DomainId` header is required (no API key). The website JS bundle calls
    #   ${apiBaseUrl}/obituaries/GetObituariesExtended
    # with header DomainId=<uuid>. For M&H:
    #   TRIBUTE_CENTER_BASE_URL  = https://api.secure.tributecenteronline.com/ClientApi
    #   TRIBUTE_CENTER_DOMAIN_ID = ee93aebe-51b2-489e-8a60-5fe98e33065b
    # `tribute_center_api_key` is retained for forward compat; unused on
    # public read paths.
    tribute_center_base_url: str = ""
    tribute_center_domain_id: str = ""
    tribute_center_api_key: str = ""

    # Outbound webhooks (Sarah → Comms Platform)
    comms_platform_webhook_url: str = ""
    comms_webhook_secret: str = ""

    # Inbound webhooks (Comms Platform → Sarah). Must match the value of
    # SARAH_WEBHOOK_SECRET on the comms-platform-backend Render env. Used
    # by the internal HTTP bridge in app/api/routes/internal.py to gate
    # calls from comms (calendar availability/book/reschedule/cancel).
    sarah_webhook_secret: str = ""

    # Inbound GHL webhooks (GHL → Sarah). Used by `/webhooks/ghl/{org_slug}/*`
    # to validate the HMAC-SHA256 signature on the `x-ghl-signature` header
    # (`sha256=<hex>`). Mirrors comms-platform-backend's `GHL_WEBHOOK_SECRET`
    # so a single secret can be reused across both. Pass-through (skip
    # validation) when empty so dev/local works without operator setup.
    ghl_webhook_secret: str = ""

    # Website crawl
    website_crawl_base_url: str = "https://www.mhfh.com"
    website_crawl_user_agent: str = "SarahBot/1.0 (McInnis & Holloway; +https://www.mhfh.com)"

    # Fallback when SMS / webhook cannot resolve tenant (multi-org revision §7–8)
    default_organization_slug: str = "mhc"
    default_location_slug: str = "park_memorial"

    # CORS
    cors_origins: str = "*"

    # Admin API
    admin_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
