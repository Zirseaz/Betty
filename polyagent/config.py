"""Application configuration loaded from environment / .env file.

Uses pydantic-settings so every value can be overridden via env-vars.
Call ``get_settings()`` to obtain the cached singleton.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# ── Resolve the .env path relative to the project root ───────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class TradingMode(StrEnum):
    """Supported trading execution modes."""

    PAPER = "paper"
    LIVE = "live"


class LLMProvider(StrEnum):
    """Supported LLM backend providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"
    DEEPSEEK = "deepseek"


class Settings(BaseSettings):
    """Central configuration for the entire PolyAgent system.

    Values are read – in descending priority – from:
    1. Explicit environment variables
    2. A ``.env`` file located at the project root
    3. The defaults declared below
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Polymarket CLOB credentials ──────────────────────────────
    poly_private_key: str = Field(default="", description="Polygon wallet private key for signing orders")
    poly_api_key: str = Field(default="", description="Polymarket CLOB API key")
    poly_api_secret: str = Field(default="", description="Polymarket CLOB API secret")
    poly_api_passphrase: str = Field(default="", description="Polymarket CLOB API passphrase")

    # ── Trading mode ─────────────────────────────────────────────
    mode: TradingMode = Field(default=TradingMode.PAPER, description="Trading execution mode")
    paper_balance: float = Field(default=10000.0, description="Starting paper balance in USDC")

    # ── Risk management ──────────────────────────────────────────
    max_position_size_usd: float = Field(default=50.0, ge=0, description="Maximum single-position size in USD")
    max_total_exposure_usd: float = Field(default=500.0, ge=0, description="Maximum total portfolio exposure in USD")
    max_drawdown_pct: float = Field(default=15.0, ge=0, le=100, description="Maximum drawdown percentage before circuit-breaker")
    min_edge_pct: float = Field(default=2.0, ge=0, description="Minimum expected edge (%) to take a trade")

    # ── LLM provider ─────────────────────────────────────────────
    llm_provider: LLMProvider = Field(default=LLMProvider.OPENAI, description="LLM backend to use for analysis")
    llm_api_key: str = Field(default="", description="API key for the chosen LLM provider")

    # ── Telegram notifications ───────────────────────────────────
    telegram_bot_token: str = Field(default="", description="Telegram bot token for notifications")
    telegram_chat_id: str = Field(default="", description="Telegram chat/group ID to send alerts to")

    # ── Scanner / scheduler ──────────────────────────────────────
    scan_interval_seconds: int = Field(default=30, ge=5, description="How often (seconds) to scan for new signals")
    target_categories: str = Field(default="all", description="Comma-separated market categories to monitor, or 'all'")

    # ── Database ─────────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/polyagent.db",
        description="Async SQLAlchemy database URL",
    )

    # ── API endpoints (not user-configurable, but centralised) ───
    polymarket_clob_url: str = Field(default="https://clob.polymarket.com", description="Polymarket CLOB API base URL")
    polymarket_gamma_url: str = Field(default="https://gamma-api.polymarket.com", description="Polymarket Gamma API base URL")
    polymarket_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        description="Polymarket WebSocket endpoint",
    )

    # ── Derived helpers ──────────────────────────────────────────

    @field_validator("target_categories", mode="before")
    @classmethod
    def _normalise_categories(cls, v: str) -> str:
        """Lower-case and strip whitespace from category list."""
        if isinstance(v, str):
            return ",".join(c.strip().lower() for c in v.split(","))
        return v

    @property
    def category_list(self) -> list[str]:
        """Return target categories as a Python list."""
        if self.target_categories == "all":
            return []
        return [c.strip() for c in self.target_categories.split(",") if c.strip()]

    @property
    def is_paper(self) -> bool:
        """Convenience flag for paper-trading mode."""
        return self.mode == TradingMode.PAPER

    @property
    def has_poly_credentials(self) -> bool:
        """Check whether Polymarket API credentials are fully configured."""
        return all([
            self.poly_private_key,
            self.poly_api_key,
            self.poly_api_secret,
            self.poly_api_passphrase,
        ])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application-wide :class:`Settings` singleton.

    The result is cached so that repeated calls are effectively free.
    To reload settings (e.g. in tests) call ``get_settings.cache_clear()``
    first.
    """
    settings = Settings()  # type: ignore[call-arg]
    logger.info(
        "Settings loaded – mode=%s  paper=%s  poly_creds=%s",
        settings.mode.value,
        settings.is_paper,
        settings.has_poly_credentials,
    )
    return settings
