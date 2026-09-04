from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from bigbot.analysis_format import analysis_display
from bigbot.database import Database
from bigbot.domain import AnalysisState, Article, Story, parse_time, utc_now
from bigbot.security import forum_title, publisher_label, safe_external_link


@dataclass(frozen=True)
class StoryFeedQuery:
    limit: int = 15
    cursor: str | None = None
    search: str = ""
    tags: tuple[str, ...] = ()


async def build_story_feed(
    database: Database,
    *,
    public_site_url: str,
    query: StoryFeedQuery | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    request = query or StoryFeedQuery(limit=limit or 15)
    decoded_cursor = _decode_cursor(request.cursor) if request.cursor else None
    stories = await database.browse_published_stories(
        limit=request.limit + 1,
        cursor=decoded_cursor,
        search=request.search,
        tags=request.tags,
    )
    has_more = len(stories) > request.limit
    visible = stories[: request.limit]
    items = [await _story_item(database, story, public_site_url) for story in visible]
    next_cursor = _encode_cursor(visible[-1]) if has_more and visible else None
    return {
        "version": 3,
        "generated_at": utc_now().isoformat(),
        "total": await database.count_published_stories(search=request.search, tags=request.tags),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "tag_counts": await database.published_story_tag_counts(search=request.search),
        "stories": items,
    }


async def build_story_detail(
    database: Database, *, story_id: int, public_site_url: str
) -> dict[str, object] | None:
    story = await database.get_published_story(story_id)
    if story is None:
        return None
    return {
        "version": 3,
        "generated_at": utc_now().isoformat(),
        "story": await _story_item(database, story, public_site_url),
    }


async def _story_item(database: Database, story: Story, public_site_url: str) -> dict[str, object]:
    articles = await database.story_articles(story.id)
    updates = await database.story_updates(story.id)
    related = await database.related_stories(story.id)
    primary = _primary_article(story, articles)
    displayed_analysis = analysis_display(
        story.analysis
        if story.analysis_state is AnalysisState.READY and story.analysis
        else story.summary
    )
    discord_url = (
        f"https://discord.com/channels/{story.guild_id}/{story.discord_thread_id}"
        if story.discord_thread_id is not None
        else None
    )
    return {
        "id": story.id,
        "title": forum_title(story.title),
        "analysis": displayed_analysis.body,
        "analysis_sources": [
            {"publisher": label, "url": url} for label, url in displayed_analysis.sources
        ],
        "analysis_state": story.analysis_state.value,
        "state": story.state.value,
        "tags": list(story.tags),
        "published_at": _published_at(story, primary),
        "updated_at": story.last_updated_at.isoformat(),
        "discord_url": discord_url,
        "web_url": _story_url(public_site_url, story.id),
        "original": _source_item(primary) if primary is not None else None,
        "sources": [_source_item(article) for article in articles],
        "updates": [
            {
                **_source_item(update.article),
                "kind": "major" if update.kind == "major_update" else "source",
                "recorded_at": update.recorded_at.isoformat(),
            }
            for update in updates
        ],
        "related": [
            {
                "id": candidate.id,
                "title": forum_title(candidate.title),
                "discord_url": (
                    f"https://discord.com/channels/{candidate.guild_id}/"
                    f"{candidate.discord_thread_id}"
                    if candidate.discord_thread_id is not None
                    else None
                ),
                "web_url": _story_url(public_site_url, candidate.id),
            }
            for candidate in related
        ],
    }


def _encode_cursor(story: Story) -> str:
    payload = json.dumps(
        [story.last_updated_at.isoformat(), story.id], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, int]:
    if len(value) > 256:
        raise ValueError("cursor is too long")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        timestamp = parse_time(str(decoded[0]))
        story_id = int(decoded[1])
        if timestamp is None or story_id < 1:
            raise ValueError
    except (
        ValueError,
        TypeError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise ValueError("invalid cursor") from exc
    return timestamp, story_id


def _story_url(public_site_url: str, story_id: int) -> str:
    return f"{public_site_url.rstrip('/')}/news/story/{story_id}/"


def _primary_article(story: Story, articles: list[Article]) -> Article | None:
    return next(
        (article for article in articles if article.id == story.primary_article_id),
        articles[0] if articles else None,
    )


def _published_at(story: Story, primary: Article | None) -> str:
    value = (
        (primary.published_at or primary.discovered_at)
        if primary is not None
        else (story.first_published_at or story.last_updated_at)
    )
    return value.isoformat()


def _source_item(article: Article) -> dict[str, object]:
    return {
        "publisher": publisher_label(article.publisher, article.url),
        "title": forum_title(article.title),
        "url": safe_external_link(article.url),
        "published_at": (
            article.published_at.isoformat() if article.published_at is not None else None
        ),
    }
