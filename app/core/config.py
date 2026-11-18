"""
Application settings, read from environment variables (and .env).
"""

import os
from functools import lru_cache
from typing import Literal
from app import __version__
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="")

    # Application settings
    APP_NAME: str = Field(default="AI Catalogs")
    APP_VERSION: str = Field(default=__version__)
    APP_DESCRIPTION: str = Field(default="Curated, daily-rotating AI catalogs for Stremio")
    PUBLIC_BASE_URL: str | None = Field(
        default=None, description="Externally reachable base URL, e.g. https://catalogs.example.org (no trailing slash)"
    )

    # Curated catalogs
    DATABASE_PATH: str = Field(
        default="/data/ai-catalogs.sqlite", description="SQLite database; must be on a local filesystem"
    )
    ADDON_ACCESS_TOKEN: str | None = Field(
        default=None,
        description="Secret path segment in /c/{token}/ URLs. Unset: the segment is 'public'",
    )
    CURATED_ADDON_ID: str = Field(default="community.curated-catalogs", description="Manifest id of the curated addon")
    CURATED_ADDON_NAME: str = Field(default="AI Catalogs", description="Manifest name of the curated addon")
    CATALOGS_DIR: str = Field(default="catalogs.d", description="Directory of catalog definition files (*.yaml)")
    RUN_WALL_CLOCK_S: int = Field(default=1200, ge=10, description="Wall-clock cap of one refresh run, in seconds")
    BACKUP_DIR: str | None = Field(
        default=None, description="Automatic backups (nightly, before migrations). Unset: <database dir>/backups"
    )
    BACKUP_KEEP: int = Field(default=7, ge=1, description="Automatic backups kept per kind")
    WORKER_TICK_S: float = Field(default=30.0, gt=0, description="Worker loop period, in seconds")
    OPS_TOKEN: str | None = Field(
        default=None, description="Bearer token for /ops (header only). Unset: /ops answers 404"
    )
    # Browser editor at /configure (PLAN.md sections 21 and 23). Off by default. With
    # EDITOR_AUTH=password both secrets are required (create_app() refuses to start without
    # them); with EDITOR_AUTH=proxy the reverse proxy's login (its user header) is used instead.
    ENABLE_EDITOR: bool = Field(default=False, description="Mount the catalog editor at /configure")
    EDITOR_AUTH: Literal["password", "proxy"] = Field(
        default="password", description="password: the editor's own login; proxy: trust EDITOR_PROXY_USER_HEADER"
    )
    EDITOR_PROXY_USER_HEADER: str = Field(
        default="Remote-User", description="Header carrying the user name set by the proxy's login (EDITOR_AUTH=proxy)"
    )
    SESSION_SECRET: str | None = Field(default=None, description="Signs /configure session cookies; >= 32 bytes")
    ADMIN_PASSWORD: str | None = Field(default=None, description="Password of the /configure login; >= 12 characters")

    # Server settings
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000)

    # Logging
    LOG_LEVEL: str = Field(default="INFO")

    # API settings. OpenRouter (or any OpenAI-compatible API) by default; GEMINI_API_KEY alone switches
    # to Google's own OpenAI-compatible endpoint (see _gemini below).
    OPENAI_API_KEY: str | None = Field(default=None)
    GEMINI_API_KEY: str | None = Field(
        default=None, description="Google AI Studio key, used when OPENAI_API_KEY is unset"
    )
    OPENAI_BASE_URL: str = Field(default="https://openrouter.ai/api/v1")
    DEFAULT_MODEL: str = Field(
        default="google/gemini-3.5-flash-lite",
        description="Model for curated catalogs (a definition's `model` overrides it)",
    )
    MAX_MODEL_CALLS_PER_DAY: int = Field(
        default=40, ge=0, description="Global cap on outbound model attempts per UTC day (all catalogs)"
    )

    # TMDB settings
    TMDB_READ_ACCESS_TOKEN: str | None = Field(default=None, alias="TMDB_API_KEY")

    # MDBList: the owner's watched history, for catalogs with `exclude_watched: true`
    MDBLIST_API_KEY: str | None = Field(default=None, description="MDBList API key (mdblist.com/preferences)")

    @model_validator(mode="after")
    def _gemini(self):
        """GEMINI_API_KEY without OPENAI_API_KEY: talk to Google directly, same code path."""
        if self.GEMINI_API_KEY and not self.OPENAI_API_KEY:
            self.OPENAI_API_KEY = self.GEMINI_API_KEY
            if "OPENAI_BASE_URL" not in self.model_fields_set:
                self.OPENAI_BASE_URL = GEMINI_BASE_URL
            if "DEFAULT_MODEL" not in self.model_fields_set and self.DEFAULT_MODEL.startswith("google/"):
                self.DEFAULT_MODEL = self.DEFAULT_MODEL.removeprefix("google/")  # Google's own model ids
        return self

    @model_validator(mode="after")
    def _normalise(self):
        if self.PUBLIC_BASE_URL:
            self.PUBLIC_BASE_URL = self.PUBLIC_BASE_URL.rstrip("/")
        if self.ADDON_ACCESS_TOKEN is not None:
            token = self.ADDON_ACCESS_TOKEN.strip()
            if token == "":
                self.ADDON_ACCESS_TOKEN = None
            elif len(token) < 16 or not token.replace("-", "").replace("_", "").isalnum():
                raise ValueError("ADDON_ACCESS_TOKEN must be at least 16 characters of [A-Za-z0-9_-]")
            else:
                self.ADDON_ACCESS_TOKEN = token
        return self


class TestSettings(Settings):
    """
    Test-specific settings that don't load from .env file and do not
    read real environment variables (isolated from host environment).
    """

    # A dummy env_prefix, so real environment variables are ignored
    model_config = SettingsConfigDict(extra="ignore", env_file=None, env_prefix="TEST_")

    # Tests point this at a temporary file (tests/conftest.py).
    DATABASE_PATH: str = Field(default="/nonexistent/set-by-conftest.sqlite")


@lru_cache()
def get_settings() -> Settings:
    """
    Get application settings, cached for performance.

    Returns:
        Settings object with application configuration
    """
    # Use test settings if we're in a test environment
    if os.getenv("PYTEST_VERSION"):
        return TestSettings()
    return Settings()


# Export settings instance for easy access
settings = get_settings()
