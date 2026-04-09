"""
Central configuration loaded from .env via pydantic-settings.
All modules import get_settings() to access config — never use os.environ directly.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── OpenRouter LLM ────────────────────────────────────────────────────────
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-oss-20b:free"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # ── HuggingFace (optional — higher rate limits for embedding model downloads)
    huggingface_token: str = ""

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── ChromaDB ──────────────────────────────────────────────────────────────
    chroma_persist_path: str = "./chroma_data"
    chroma_embedding_model: str = "all-MiniLM-L6-v2"

    # ── MCP Server ────────────────────────────────────────────────────────────
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = 8000

    # ── FastAPI Dashboard ─────────────────────────────────────────────────────
    dashboard_port: int = 8080

    # ── Google OAuth ──────────────────────────────────────────────────────────
    google_credentials_path: str = "./secrets/credentials.json"
    google_token_path: str = "./secrets/token.json"

    # ── Filesystem ────────────────────────────────────────────────────────────
    fs_allowed_root: str = "./sandbox"

    # ── Twilio ────────────────────────────────────────────────────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    twilio_to_number: str = ""
    twilio_webhook_base_url: str = ""  # e.g. https://xxxx.ngrok.io (required for voice approval calls)

    # ── Observer ──────────────────────────────────────────────────────────────
    observer_poll_interval: int = 60

    # ── Meeting auto-schedule ─────────────────────────────────────────────────
    # Wall-clock times in meeting emails ("April 10 1pm") are interpreted in this
    # timezone (IANA name). Use UTC if your Google Calendar primary is GMT+0.
    meeting_parse_timezone: str = "UTC"

    @property
    def mcp_sse_url(self) -> str:
        return f"http://{self.mcp_server_host}:{self.mcp_server_port}/sse"

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def voice_approval_enabled(self) -> bool:
        """True when Twilio + a public webhook URL are both configured."""
        return bool(self.twilio_enabled and self.twilio_webhook_base_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
