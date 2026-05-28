from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Google APIs ─────────────────────────────────────────────────────────────
    # Calendar and Sheets use a service account — no API key needed here.
    google_service_account_path: str = "./config/service-account.json"
    google_calendar_id: str = "primary"
    google_sheet_id: str
    studio_timezone: str = "America/Los_Angeles"

    # ── LLM — Groq (fast, OpenAI-compatible) ────────────────────────────────────
    groq_api_key: str
    # llama-3.3-70b-versatile: best instruction-following, ~600ms p50 on Groq
    # llama-3.1-8b-instant:    fastest (~200ms) but weaker on complex tool chains
    groq_voice_model: str
    groq_chat_model: str
    gemini_api_key: Optional[str] = None

    # ── Vapi — Phase 2 voice agent ───────────────────────────────────────────────
    vapi_api_key: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore GEMINI_* and other legacy vars still in .env
    )


settings = Settings()
