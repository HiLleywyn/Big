from __future__ import annotations

from datetime import UTC, datetime

from bigbot.database import Database
from bigbot.domain import FeedItem, FeedKind, StoryState
from bigbot.normalization import normalize_item
from bigbot.public_api import StoryFeedQuery, build_story_detail, build_story_feed


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
    story, _ = await database.create_story_with_article(
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
    assert "Analysis sources" not in result["analysis"]
    assert result["analysis_sources"] == [
        {"publisher": "Election office", "url": "https://example.gov/results"}
    ]
    assert payload["total"] == 1
    assert payload["has_more"] is False
    assert payload["tag_counts"] == {"Politics": 1, "World": 1}

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
