"""Application configuration, loaded from environment / .env.

Secrets live in .env (never committed). Persistent data lives under /data.
"""
from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _clean_number(raw: str) -> str:
    """Normalise a phone number to digits only (strip +, spaces, @c.us, etc.)."""
    raw = raw.split("@", 1)[0]
    return "".join(ch for ch in raw if ch.isdigit())


class Settings:
    def __init__(self) -> None:
        # --- Claude ---
        self.anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
        self.claude_model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
        self.claude_max_tokens: int = int(os.getenv("CLAUDE_MAX_TOKENS", "1024"))

        # --- WAHA (WhatsApp HTTP API) ---
        self.waha_base_url: str = os.getenv("WAHA_BASE_URL", "http://localhost:3000").rstrip("/")
        self.waha_session: str = os.getenv("WAHA_SESSION", "default")
        self.waha_api_key: str = os.getenv("WAHA_API_KEY", "")

        # --- Access control ---
        # Comma-separated list of allowed WhatsApp numbers (any format; normalised to digits).
        self.allowed_numbers: set[str] = {
            _clean_number(n) for n in os.getenv("ALLOWED_NUMBERS", "").split(",") if n.strip()
        }

        # --- Data / persistence ---
        self.db_path: str = os.getenv("DB_PATH", "/data/app.sqlite")

        # --- Defaults ---
        self.default_timezone: str = os.getenv("DEFAULT_TIMEZONE", "America/New_York")

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    def is_allowed(self, number: str) -> bool:
        return _clean_number(number) in self.allowed_numbers


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Convenience module-level singleton.
settings = get_settings()
clean_number = _clean_number
