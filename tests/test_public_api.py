from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from bigbot.database import Database
from bigbot.domain import Article, DeliveryState, FeedItem, FeedKind, StoryState
from bigbot.normalization import normalize_item
from bigbot.public_api import (
    StoryFeedQuery,
    _analysis_source_items,
    _unique_articles,
    build_story_detail,
    build_story_feed,
)


async def test_public_feed_mirrors_published_story_sources_and_discord_link(tmp_path) -> None:
    database = Database(tmp_path / "big.db")
    await database.connect()
    feed = await database.add_feed(
        guild_id=10,
        forum_channel_id=20,
        name="Wire",
        kind=FeedKind.RSS,
        source="https://example.com/feed.xml",
        interval_seconds=900,
        tag_ids=(),
        default_tags=("World",),
        include_replies=False,
        include_reposts=False,
        created_by=30,
    )
    published = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    item = FeedItem(
        "wire-1",
        "Country holds election",
        "https://example.com/story",
        "Votes are being counted.",
        "Wire",
        published,
    )
    normalized = normalize_item(item, fallback_publisher=feed.publisher)
    story, original = await database.create_story_with_article(
        feed=feed,
        item=item,
        normalized=normalized,
        tags=("Politics", "World"),
        state=StoryState.NEW,
        priority=90,
    )
    await database.mark_story_published(story.id, thread_id=40, message_id=50)
    await database.save_story_analysis(
        story.id,
        analysis=(
            "**Summary**\nVotes are being counted.\n\n**Key facts**\n- Polls have closed.\n\n"
            "**Analysis sources**\n- [Election office](https://example.gov/results)"
        ),
        related_story_ids=(),
    )
    update_item = FeedItem(
        "wire-2",
        "Election officials publish an initial count",
        "https://example.com/story-update",
        "The first official count has been published.",
        "Wire",
        datetime(2026, 9, 2, 13, 0, tzinfo=UTC),
    )
    latest_story = (await database.get_story(story.id)) or story
    _, update_article = await database.attach_article(
        story=latest_story,
        feed=feed,
        item=update_item,
        normalized=normalize_item(update_item, fallback_publisher=feed.publisher),
        tags=("Politics", "World"),
        state=StoryState.DEVELOPING,
        priority=90,
        significant=True,
    )
    await database.save_story_update_detail(
        story.id,
        update_article.id,
        "Officials confirmed that the first count is now available.",
    )

    payload = await build_story_feed(
        database,
        query=StoryFeedQuery(limit=10),
        public_site_url="https://bigif.org",
    )
    stories = payload["stories"]
    assert isinstance(stories, list)
    assert len(stories) == 1
    result = stories[0]
    assert isinstance(result, dict)
    assert result["discord_url"] == "https://discord.com/channels/10/40"
    assert result["web_url"] == f"https://bigif.org/news/story/{story.id}/"
    assert result["tags"] == ["Politics", "World"]
    assert result["published_at"] == published.isoformat()
    assert result["sources"][0]["publisher"] == "Wire"
    assert result["original"]["url"] == original.url
    assert result["original"]["description"] == "Votes are being counted."
    assert result["updates"] == [
        {
            "publisher": "Wire",
            "title": "Election officials publish an initial count",
            "description": "The first official count has been published.",
            "url": "https://example.com/story-update",
            "published_at": "2026-09-02T13:00:00+00:00",
            "detail": "Officials confirmed that the first count is now available.",
            "kind": "major",
            "recorded_at": result["updates"][0]["recorded_at"],
        }
    ]
    assert "Analysis sources" not in result["analysis"]
    assert result["analysis_sources"] == [
        {"publisher": "Election office", "url": "https://example.gov/results"}
    ]
    assert payload["total"] == 1
    assert payload["has_more"] is False
    assert payload["tag_counts"] == {"Politics": 1, "World": 1}
    week_start = datetime(2026, 8, 30, tzinfo=UTC)
    weekly = await database.save_weekly_summary(
        guild_id=10,
        forum_channel_id=20,
        week_start=week_start,
        week_end=published,
        title="Weekly Summary | Aug 30 to Sep 2, 2026",
        overview="The most-covered stories this week.",
        story_ids=(story.id,),
    )
    await database.mark_weekly_summary_published(weekly.id, thread_id=60, message_id=70)
    with_weekly = await build_story_feed(
        database,
        query=StoryFeedQuery(limit=10),
        public_site_url="https://bigif.org",
    )
    weekly_item = with_weekly["weekly_summary"]
    assert isinstance(weekly_item, dict)
    assert weekly_item["discord_url"] == "https://discord.com/channels/10/60"
    weekly_story = weekly_item["stories"][0]
    assert weekly_story["id"] == story.id
    assert weekly_story["web_url"] == f"https://bigif.org/news/story/{story.id}/"
    assert weekly_story["source_count"] == 2
    assert weekly_story["key_facts"] == ["Polls have closed."]
    assert "discord_url" not in weekly_story

    empty_weekly = await database.save_weekly_summary(
        guild_id=10,
        forum_channel_id=99,
        week_start=week_start,
        week_end=published,
        title="Empty secondary weekly summary",
        overview="No public stories remain.",
        story_ids=(9999,),
    )
    await database.mark_weekly_summary_published(empty_weekly.id, thread_id=80, message_id=90)
    still_valid = await build_story_feed(
        database,
        query=StoryFeedQuery(limit=10),
        public_site_url="https://bigif.org",
    )
    assert still_valid["weekly_summary"]["id"] == weekly.id

    detail = await build_story_detail(
        database, story_id=story.id, public_site_url="https://bigif.org"
    )
    assert detail is not None
    assert detail["story"] == result
    assert (
        await build_story_detail(database, story_id=9999, public_site_url="https://bigif.org")
        is None
    )
    await database.close()


def test_public_api_collapses_google_news_transport_copies() -> None:
    published = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)
    original = Article(
        id=1,
        feed_id=1,
        story_id=1,
        external_id="rss-copy",
        publisher="Reuters",
        title="One event - Reuters",
        url="https://news.google.com/rss/articles/ABC?oc=5",
        canonical_url="https://news.google.com/rss/articles/ABC?oc=5",
        published_at=published,
        description="One event",
        discovered_at=published,
        normalized_title="one event",
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        fingerprint="rss",
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
    )
    duplicate = replace(
        original,
        id=2,
        external_id="atom-copy",
        url="https://news.google.com/atom/articles/ABC?oc=5",
        canonical_url="https://news.google.com/atom/articles/ABC?oc=5",
        fingerprint="atom",
    )

    assert _unique_articles([original, duplicate]) == (original,)
    assert _analysis_source_items((("Google News", duplicate.url),), [original, duplicate]) == []


async def test_public_feed_paginates_and_filters_by_search_and_tag(tmp_path) -> None:
    database = Database(tmp_path / "big.db")
    await database.connect()
    feed = await database.add_feed(
        guild_id=10,
        forum_channel_id=20,
        name="Wire",
        kind=FeedKind.RSS,
        source="https://example.com/feed.xml",
        interval_seconds=900,
        tag_ids=(),
        default_tags=(),
        include_replies=False,
        include_reposts=False,
        created_by=30,
    )
    for index, (title, publisher, tags) in enumerate(
        (
            ("Chip company opens new plant", "Tech Wire", ("Technology",)),
            ("Central bank holds rates", "Market Desk", ("Markets",)),
            ("Election board certifies vote", "Civic Wire", ("Politics",)),
        ),
        start=1,
    ):
        item = FeedItem(
            f"item-{index}",
            title,
            f"https://example.com/{index}",
            f"Report {index}",
            publisher,
            datetime(2026, 9, 2, 12, index, tzinfo=UTC),
            publisher=publisher,
        )
        normalized = normalize_item(item, fallback_publisher=publisher)
        story, _ = await database.create_story_with_article(
            feed=feed,
            item=item,
            normalized=normalized,
            tags=tags,
            state=StoryState.NEW,
            priority=10,
        )
        await database.mark_story_published(story.id, thread_id=40 + index, message_id=50)

    first = await build_story_feed(
        database,
        query=StoryFeedQuery(limit=2),
        public_site_url="https://bigif.org",
    )
    assert first["has_more"] is True
    assert len(first["stories"]) == 2
    assert isinstance(first["next_cursor"], str)

    second = await build_story_feed(
        database,
        query=StoryFeedQuery(limit=2, cursor=str(first["next_cursor"])),
        public_site_url="https://bigif.org",
    )
    assert len(second["stories"]) == 1
    assert second["has_more"] is False

    filtered = await build_story_feed(
        database,
        query=StoryFeedQuery(limit=15, search="Market", tags=("Markets",)),
        public_site_url="https://bigif.org",
    )
    assert filtered["total"] == 1
    result = filtered["stories"]
    assert isinstance(result, list)
    assert result[0]["title"] == "Central bank holds rates"
    await database.close()
