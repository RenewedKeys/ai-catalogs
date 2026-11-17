"""
Application configuration settings for the Stremio AI Companion.
"""

import os
from functools import lru_cache
from app import __version__
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.models.enums import Languages


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_prefix="")

    # Application settings
    APP_NAME: str = Field(default="AI Catalogs")
    APP_VERSION: str = Field(default=__version__)
    APP_DESCRIPTION: str = Field(default="Curated, daily-rotating AI catalogs for Stremio")
    STREMIO_ADDON_ID: str = Field(
        default="community.ai-catalogs.legacy", description="Stremio addon ID of the legacy (upstream) addon"
    )
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
    # Browser editor at /admin (PLAN.md section 21). Off by default; when on, both secrets are
    # required (create_app() refuses to start without them).
    ENABLE_EDITOR: bool = Field(default=False, description="Mount the catalog editor at /admin")
    SESSION_SECRET: str | None = Field(default=None, description="Signs /admin session cookies; >= 32 bytes")
    ADMIN_PASSWORD: str | None = Field(default=None, description="Password of the /admin login; >= 12 characters")

    # Legacy (upstream stremio-ai-companion) surface: /configure, /save-config, /config/{blob}/...
    # Off by default. Only enable it on a trusted network.
    ENABLE_LEGACY: bool = Field(default=False, description="Mount the upstream configure UI and per-user routes")

    # Server settings
    HOST: str = Field(default="0.0.0.0")
    PORT: int = Field(default=8000)

    # Logging
    LOG_LEVEL: str = Field(default="INFO")

    # Security
    ENCRYPTION_KEY: str | None = Field(
        default=None, description="Encryption key for legacy config URLs; required when ENABLE_LEGACY is true"
    )

    # API settings
    OPENAI_API_KEY: str | None = Field(default=None)
    OPENAI_BASE_URL: str = Field(default="https://openrouter.ai/api/v1")
    DEFAULT_MODEL: str = Field(
        default="google/gemini-3.5-flash-lite",
        description="Model for curated catalogs (per-catalog `model` overrides it) and legacy",
    )
    MAX_MODEL_CALLS_PER_DAY: int = Field(
        default=40, ge=0, description="Global cap on outbound model attempts per UTC day (all catalogs)"
    )

    # TMDB settings
    TMDB_READ_ACCESS_TOKEN: str | None = Field(default=None, alias="TMDB_API_KEY")

    # MDBList: the owner's watched history, for catalogs with `exclude_watched: true`
    MDBLIST_API_KEY: str | None = Field(default=None, description="MDBList API key (mdblist.com/preferences)")

    # RPDB settings
    RPDB_API_KEY: str | None = Field(default=None)

    # Catalog settings
    PREFERRED_USER_SEARCH_LANGUAGE: str | None = Field(
        default=Languages.EN.code, description="Preferred language for user search results"
    )
    MAX_CATALOG_RESULTS: int = Field(default=10, description="Maximum number of results to return in cached catalog")
    MAX_CATALOG_ENTRIES: int = Field(default=100, description="Maximum total entries to store per catalog")

    # Manifest settings
    SPLIT_MANIFESTS: bool = Field(default=False, description="Enable split manifests for movie/series types")
    ENABLE_FEED_CATALOGS: bool = Field(default=True, description="Expose predefined feed catalogs in manifest")
    REDIS_HOST: str | None = Field(default=None, description="Redis host if URL not provided")
    REDIS_PORT: int = Field(default=6379, description="Redis port")
    REDIS_DB: int = Field(default=0, description="Redis DB index")
    CACHE_SEARCH_QUERY_TTL: int = Field(default=14400, description="TTL for cached search queries")  # default 4h

    # Customizing
    FOOTER_ENABLED: bool = Field(default=True, description="Show footer on pages")

    @model_validator(mode="after")
    def _legacy_needs_encryption_key(self):
        if self.ENABLE_LEGACY and (not self.ENCRYPTION_KEY or len(self.ENCRYPTION_KEY) < 4):
            raise ValueError("ENCRYPTION_KEY (at least 4 characters) is required when ENABLE_LEGACY is true")
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

    FOOTER_ENABLED: bool = Field(default=True)

    # Use a dummy env_prefix so real env vars like REDIS_HOST are ignored
    model_config = SettingsConfigDict(extra="ignore", env_file=None, env_prefix="TEST_")

    # Override required field with test default
    ENCRYPTION_KEY: str | None = Field(default="test-encryption-key-for-testing")

    # The inherited test suite exercises the legacy routes through the module-level app.
    # Tests of the default (legacy off) behaviour build their own app with create_app().
    ENABLE_LEGACY: bool = Field(default=True)

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
