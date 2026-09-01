from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class FeedSpec:
    name: str
    url: str
    publisher: str
    default_tags: tuple[str, ...]
    interval_seconds: int
    forum_channel_id: int | None = None


@dataclass(frozen=True)
class ClusteringOptions:
    threshold: float = 0.68
    window_hours: int = 72
    stale_after_hours: int = 96


@dataclass(frozen=True)
class UpdateOptions:
    post_major_updates: bool = True
    post_source_updates: bool = False


@dataclass(frozen=True)
class RetentionOptions:
    clear_after_days: int | None = None
    action: str = "archive"
    batch_size: int = 25


@dataclass(frozen=True)
class AppConfig:
    guild_id: int | None = None
    forum_channel_id: int | None = None
    polling_interval_seconds: int = 900
    clustering: ClusteringOptions = ClusteringOptions()
    updates: UpdateOptions = UpdateOptions()
    retention: RetentionOptions = RetentionOptions()
    tag_mappings: dict[str, tuple[str, ...]] | None = None
    source_priorities: dict[str, int] | None = None
    feeds: tuple[FeedSpec, ...] = ()


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
    config_path: Path
    app_config: AppConfig
    dry_run: bool
    health_host: str
    health_port: int

    def validate(self, *, require_discord: bool = False) -> None:
        if require_discord and not self.dry_run and not self.discord_token:
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
        if not 1024 <= self.health_port <= 65535:
            raise ConfigurationError("BIG_HEALTH_PORT must be between 1024 and 65535")
        clustering = self.app_config.clustering
        if not 0.5 <= clustering.threshold <= 0.95:
            raise ConfigurationError("clustering.threshold must be between 0.5 and 0.95")
        if not 1 <= clustering.window_hours <= 720:
            raise ConfigurationError("clustering.window_hours must be between 1 and 720")
        if not 1 <= clustering.stale_after_hours <= 8760:
            raise ConfigurationError("clustering.stale_after_hours must be between 1 and 8760")
        retention = self.app_config.retention
        if retention.clear_after_days is not None and not 1 <= retention.clear_after_days <= 3650:
            raise ConfigurationError("retention.clear_after_days must be between 1 and 3650")
        if retention.action not in {"archive", "delete"}:
            raise ConfigurationError("retention.action must be archive or delete")
        if not 1 <= retention.batch_size <= 100:
            raise ConfigurationError("retention.batch_size must be between 1 and 100")
        for feed in self.app_config.feeds:
            if not 300 <= feed.interval_seconds <= 86400:
                raise ConfigurationError(
                    f"feed {feed.name!r} interval_seconds must be between 300 and 86400"
                )


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


def load_app_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"could not read YAML config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("YAML config root must be a mapping")
    clustering_raw = _mapping(raw.get("clustering"), "clustering")
    updates_raw = _mapping(raw.get("update_behavior"), "update_behavior")
    retention_raw = _mapping(raw.get("retention"), "retention")
    default_interval = _positive_int(
        raw.get("polling_interval_seconds", 900), "polling_interval_seconds"
    )
    feeds: list[FeedSpec] = []
    feed_values = raw.get("feeds", [])
    if not isinstance(feed_values, list):
        raise ConfigurationError("feeds must be a list")
    for index, value in enumerate(feed_values):
        item = _mapping(value, f"feeds[{index}]")
        name = _text(item.get("name"), f"feeds[{index}].name")
        url = _text(item.get("url"), f"feeds[{index}].url")
        publisher = _text(item.get("publisher") or name, f"feeds[{index}].publisher")
        tags = _text_tuple(item.get("default_tags", []), f"feeds[{index}].default_tags")
        forum_raw = item.get("forum_channel_id")
        feeds.append(
            FeedSpec(
                name=name,
                url=url,
                publisher=publisher,
                default_tags=tags,
                interval_seconds=_positive_int(
                    item.get("interval_seconds", default_interval),
                    f"feeds[{index}].interval_seconds",
                ),
                forum_channel_id=(
                    _positive_int(forum_raw, f"feeds[{index}].forum_channel_id")
                    if forum_raw is not None
                    else None
                ),
            )
        )
    tag_raw = _mapping(raw.get("tag_mappings"), "tag_mappings")
    tag_mappings = {
        str(tag): _text_tuple(terms, f"tag_mappings.{tag}") for tag, terms in tag_raw.items()
    }
    priorities_raw = _mapping(raw.get("source_priorities"), "source_priorities")
    priorities = {
        str(name): _positive_int(value, f"source_priorities.{name}")
        for name, value in priorities_raw.items()
    }
    return AppConfig(
        guild_id=_optional_positive_int(raw.get("guild_id"), "guild_id"),
        forum_channel_id=_optional_positive_int(raw.get("forum_channel_id"), "forum_channel_id"),
        polling_interval_seconds=default_interval,
        clustering=ClusteringOptions(
            threshold=_number(clustering_raw.get("threshold", 0.68), "clustering.threshold"),
            window_hours=_positive_int(
                clustering_raw.get("window_hours", 72), "clustering.window_hours"
            ),
            stale_after_hours=_positive_int(
                clustering_raw.get("stale_after_hours", 96), "clustering.stale_after_hours"
            ),
        ),
        updates=UpdateOptions(
            post_major_updates=_yaml_boolean(
                updates_raw.get("post_major_updates", True),
                "update_behavior.post_major_updates",
            ),
            post_source_updates=_yaml_boolean(
                updates_raw.get("post_source_updates", False),
                "update_behavior.post_source_updates",
            ),
        ),
        retention=RetentionOptions(
            clear_after_days=_optional_positive_int(
                retention_raw.get("clear_after_days"),
                "retention.clear_after_days",
            ),
            action=_retention_action(retention_raw.get("action", "archive")),
            batch_size=_positive_int(
                retention_raw.get("batch_size", 25),
                "retention.batch_size",
            ),
        ),
        tag_mappings=tag_mappings,
        source_priorities=priorities,
        feeds=tuple(feeds),
    )


def _mapping(value: object, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ConfigurationError(f"{name} is required")
    return text


def _text_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{name} must be a list")
    return tuple(_text(item, name) for item in value)


def _positive_int(value: object, name: str) -> int:
    try:
        number = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if number <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return number


def _number(value: object, name: str) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


def _yaml_boolean(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigurationError(f"{name} must be true or false")


def _retention_action(value: object) -> str:
    action = str(value or "").strip().lower()
    if action not in {"archive", "delete"}:
        raise ConfigurationError("retention.action must be archive or delete")
    return action


def _optional_positive_int(value: object, name: str) -> int | None:
    return None if value in {None, ""} else _positive_int(value, name)


def load_settings(*, require_discord: bool = False) -> Settings:
    load_dotenv(override=False)
    guild_raw = os.getenv("BIG_GUILD_ID", "").strip()
    config_path = Path(os.getenv("BIG_CONFIG_PATH", "config.yaml"))
    app_config = load_app_config(config_path)
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
        config_path=config_path,
        app_config=app_config,
        dry_run=_boolean("BIG_DRY_RUN", "false"),
        health_host=os.getenv("BIG_HEALTH_HOST", "0.0.0.0").strip(),
        health_port=_integer("BIG_HEALTH_PORT", "8787"),
    )
    settings.validate(require_discord=require_discord)
    return settings
