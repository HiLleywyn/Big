from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    discord_token: str | None
    x_bearer_token: str | None
    openrouter_api_key: str | None
    openrouter_model: str
    ai_web_search: bool
    ai_zdr: bool
    database_path: Path
    guild_id: int | None
    poll_tick_seconds: int
    max_backfill: int
    http_timeout_seconds: int
    rss_max_bytes: int
    log_level: str

    def validate(self, *, require_discord: bool = False) -> None:
        if require_discord and not self.discord_token:
            raise ConfigurationError("DISCORD_TOKEN is required to run Big")
        if self.guild_id is not None and self.guild_id <= 0:
            raise ConfigurationError("BIG_GUILD_ID must be a positive Discord snowflake")
        if not 5 <= self.poll_tick_seconds <= 3600:
            raise ConfigurationError("BIG_POLL_TICK_SECONDS must be between 5 and 3600")
        if not 1 <= self.max_backfill <= 25:
            raise ConfigurationError("BIG_MAX_BACKFILL must be between 1 and 25")
        if not 3 <= self.http_timeout_seconds <= 120:
            raise ConfigurationError("BIG_HTTP_TIMEOUT_SECONDS must be between 3 and 120")
        if not 65536 <= self.rss_max_bytes <= 10 * 1024 * 1024:
            raise ConfigurationError("BIG_RSS_MAX_BYTES must be between 64 KiB and 10 MiB")
        if self.log_level not in logging.getLevelNamesMapping():
            raise ConfigurationError("BIG_LOG_LEVEL is invalid")
        if not self.openrouter_model or len(self.openrouter_model) > 200:
            raise ConfigurationError("BIG_OPENROUTER_MODEL is invalid")


def _integer(name: str, default: str) -> int:
    value = os.getenv(name, default).strip()
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _boolean(name: str, default: str) -> bool:
    value = os.getenv(name, default).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def load_settings(*, require_discord: bool = False) -> Settings:
    load_dotenv(override=False)
    guild_raw = os.getenv("BIG_GUILD_ID", "").strip()
    settings = Settings(
        discord_token=os.getenv("DISCORD_TOKEN") or None,
        x_bearer_token=os.getenv("X_BEARER_TOKEN") or None,
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY") or None,
        openrouter_model=os.getenv("BIG_OPENROUTER_MODEL", "openrouter/auto").strip(),
        ai_web_search=_boolean("BIG_AI_WEB_SEARCH", "true"),
        ai_zdr=_boolean("BIG_AI_ZDR", "true"),
        database_path=Path(os.getenv("BIG_DATABASE_PATH", "data/big.db")),
        guild_id=_integer("BIG_GUILD_ID", guild_raw) if guild_raw else None,
        poll_tick_seconds=_integer("BIG_POLL_TICK_SECONDS", "15"),
        max_backfill=_integer("BIG_MAX_BACKFILL", "3"),
        http_timeout_seconds=_integer("BIG_HTTP_TIMEOUT_SECONDS", "15"),
        rss_max_bytes=_integer("BIG_RSS_MAX_BYTES", "2097152"),
        log_level=os.getenv("BIG_LOG_LEVEL", "INFO").strip().upper(),
    )
    settings.validate(require_discord=require_discord)
    return settings
