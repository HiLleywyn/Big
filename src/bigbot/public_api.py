from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from bigbot.analysis_format import analysis_display, story_update_detail, visible_story_updates
from bigbot.database import Database
from bigbot.domain import AnalysisState, Article, Story, WeeklySummary, parse_time, utc_now
from bigbot.normalization import normalize_url
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
        "version": 5,
        "generated_at": utc_now().isoformat(),
        "total": await database.count_published_stories(search=request.search, tags=request.tags),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "tag_counts": await database.published_story_tag_counts(search=request.search),
        "weekly_summary": await _latest_weekly_summary_item(database, public_site_url),
        "stories": items,
    }


async def build_story_detail(
    database: Database, *, story_id: int, public_site_url: str
) -> dict[str, object] | None:
    story = await database.get_published_story(story_id)
    if story is None:
        return None
    return {
        "version": 5,
        "generated_at": utc_now().isoformat(),
        "story": await _story_item(database, story, public_site_url),
    }


async def _story_item(database: Database, story: Story, public_site_url: str) -> dict[str, object]:
    articles = await database.story_articles(story.id)
    updates = await database.story_updates(story.id)
    related = await database.related_stories(story.id)
    primary = _primary_article(story, articles)
    visible_updates = visible_story_updates(primary, updates)
    displayed_analysis = analysis_display(
        story.analysis
        if story.analysis_state is AnalysisState.READY and story.analysis
        else story.summary,
        title=story.title,
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
        "analysis_sources": _analysis_source_items(displayed_analysis.sources, articles),
        "analysis_state": story.analysis_state.value,
        "state": story.state.value,
        "tags": list(story.tags),
        "published_at": _published_at(story, primary),
        "updated_at": story.last_updated_at.isoformat(),
        "discord_url": discord_url,
        "web_url": _story_url(public_site_url, story.id),
        "original": _source_item(primary) if primary is not None else None,
        "sources": [_source_item(article) for article in _unique_articles(articles)],
        "updates": [
            {
                **_source_item(update.article),
                "detail": update.detail
                or story_update_detail(
                    update.article.title, update.article.description, limit=1000
                ),
                "kind": "major" if update.kind == "major_update" else "source",
                "recorded_at": update.recorded_at.isoformat(),
            }
            for update in visible_updates
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


async def _latest_weekly_summary_item(
    database: Database, public_site_url: str
) -> dict[str, object] | None:
    summary = await database.latest_weekly_summary()
    if summary is None:
        return None
    stories: list[Story] = []
    for story_id in summary.story_ids:
        story = await database.get_published_story(story_id)
        if story is not None:
            stories.append(story)
    return _weekly_summary_item(summary, stories, public_site_url)


def _weekly_summary_item(
    summary: WeeklySummary, stories: list[Story], public_site_url: str
) -> dict[str, object]:
    discord_url = (
        f"https://discord.com/channels/{summary.guild_id}/{summary.discord_thread_id}"
        if summary.discord_thread_id is not None
        else None
    )
    return {
        "id": summary.id,
        "title": summary.title,
        "overview": summary.overview,
        "week_start": summary.week_start.isoformat(),
        "week_end": summary.week_end.isoformat(),
        "generated_at": summary.generated_at.isoformat(),
        "discord_url": discord_url,
        "web_url": f"{public_site_url.rstrip('/')}/news/",
        "stories": [
            {
                "id": story.id,
                "title": forum_title(story.title),
                "summary": _summary_text(story),
                "tags": list(story.tags),
                "web_url": _story_url(public_site_url, story.id),
                "discord_url": (
                    f"https://discord.com/channels/{story.guild_id}/{story.discord_thread_id}"
                    if story.discord_thread_id is not None
                    else None
                ),
            }
            for story in stories
        ],
    }


def _summary_text(story: Story) -> str:
    value = (
        story.analysis
        if story.analysis_state is AnalysisState.READY and story.analysis
        else story.summary
    )
    body = analysis_display(value, title=story.title).body
    lines: list[str] = []
    in_summary = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        heading = line.replace("**", "").casefold()
        if heading == "summary":
            in_summary = True
            continue
        if in_summary and heading in {"key facts", "context", "unclear or disputed"}:
            break
        if in_summary and line:
            lines.append(line.removeprefix("- "))
    result = " ".join(lines).strip() or story.summary
    return result[:500].rstrip()


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


def _unique_articles(articles: list[Article]) -> tuple[Article, ...]:
    unique: list[Article] = []
    seen: set[str] = set()
    for article in articles:
        key = normalize_url(article.canonical_url or article.url)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(article)
    return tuple(unique)


def _analysis_source_items(
    sources: tuple[tuple[str, str], ...], articles: list[Article]
) -> list[dict[str, str]]:
    article_urls = {normalize_url(article.canonical_url or article.url) for article in articles}
    visible: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, url in sources:
        key = normalize_url(url)
        if not key or key in seen or key in article_urls:
            continue
        seen.add(key)
        visible.append({"publisher": label, "url": url})
    return visible
