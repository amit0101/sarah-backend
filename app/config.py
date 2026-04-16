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

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Google Calendar — path to service account JSON or OAuth credentials file
    google_calendar_credentials: str = ""
    google_calendar_delegation_email: Optional[str] = None

    # Tribute Center / obituaries
    tribute_center_api_key: str = ""
    tribute_center_base_url: str = ""

    # Outbound webhooks (Comms Platform)
    comms_platform_webhook_url: str = ""
    comms_webhook_secret: str = ""

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
