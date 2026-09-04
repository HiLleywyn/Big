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
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class StoryState(StrEnum):
    NEW = "new"
    DEVELOPING = "developing"
    BREAKING = "breaking"
    UPDATED = "updated"
    STALE = "stale"
    MERGED = "merged"


class PublicationState(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class AnalysisState(StrEnum):
    DISABLED = "disabled"
    READY = "ready"
    FAILED = "failed"


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
    default_tags: tuple[str, ...] = ()
    failure_count: int = 0
    publisher: str = ""
    summarization_enabled: bool = True


@dataclass(frozen=True)
class FeedItem:
    external_id: str
    title: str
    url: str
    summary: str
    author: str | None
    published_at: datetime | None
    image_url: str | None = None
    publisher: str | None = None


@dataclass(frozen=True)
class FetchResult:
    items: tuple[FeedItem, ...]
    cursor: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


@dataclass(frozen=True)
class Article:
    id: int
    feed_id: int | None
    story_id: int | None
    external_id: str
    publisher: str
    title: str
    url: str
    canonical_url: str
    published_at: datetime | None
    description: str
    discovered_at: datetime
    normalized_title: str
    entities: tuple[str, ...]
    keywords: tuple[str, ...]
    numbers: tuple[str, ...]
    event_terms: tuple[str, ...]
    fingerprint: str
    delivery_state: DeliveryState
    delivery_error: str | None


@dataclass(frozen=True)
class Story:
    id: int
    guild_id: int
    forum_channel_id: int
    title: str
    summary: str
    state: StoryState
    publication_state: PublicationState
    discord_thread_id: int | None
    discord_starter_message_id: int | None
    tags: tuple[str, ...]
    normalized_title: str
    entities: tuple[str, ...]
    keywords: tuple[str, ...]
    numbers: tuple[str, ...]
    event_terms: tuple[str, ...]
    first_published_at: datetime | None
    last_published_at: datetime | None
    last_updated_at: datetime
    merged_into_story_id: int | None = None
    primary_article_id: int | None = None
    primary_priority: int = 0
    analysis: str | None = None
    analysis_state: AnalysisState = AnalysisState.DISABLED
    analysis_error: str | None = None
    analysis_updated_at: datetime | None = None


@dataclass(frozen=True)
class StoryUpdate:
    article: Article
    kind: str
    recorded_at: datetime


@dataclass(frozen=True)
class PublishReceipt:
    thread_id: int
    message_id: int
