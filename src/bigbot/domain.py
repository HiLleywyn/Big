from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class FeedKind(StrEnum):
    RSS = "rss"
    X = "x"


class FeedState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class DeliveryState(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    SKIPPED = "skipped"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class Feed:
    id: int
    guild_id: int
    forum_channel_id: int
    name: str
    kind: FeedKind
    source: str
    interval_seconds: int
    tag_ids: tuple[int, ...]
    include_replies: bool
    include_reposts: bool
    state: FeedState
    cursor: str | None
    etag: str | None
    last_modified: str | None
    next_poll_at: datetime
    last_polled_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class FeedItem:
    external_id: str
    title: str
    url: str
    summary: str
    author: str | None
    published_at: datetime | None
    image_url: str | None = None


@dataclass(frozen=True)
class FetchResult:
    items: tuple[FeedItem, ...]
    cursor: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass(frozen=True)
class PublishReceipt:
    thread_id: int
    message_id: int
