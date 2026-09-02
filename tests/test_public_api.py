from __future__ import annotations

from datetime import UTC, datetime

from bigbot.database import Database
from bigbot.domain import FeedItem, FeedKind, StoryState
from bigbot.normalization import normalize_item
from bigbot.public_api import build_story_feed


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

    payload = await build_story_feed(database, limit=10, public_site_url="https://bigif.org")
    stories = payload["stories"]
    assert isinstance(stories, list)
    assert len(stories) == 1
    result = stories[0]
    assert isinstance(result, dict)
    assert result["discord_url"] == "https://discord.com/channels/10/40"
    assert result["web_url"] == f"https://bigif.org/news/#story-{story.id}"
    assert result["tags"] == ["Politics", "World"]
    assert result["published_at"] == published.isoformat()
    assert result["sources"][0]["publisher"] == "Wire"
    assert "Analysis sources" not in result["analysis"]
    assert result["analysis_sources"] == [
        {"publisher": "Election office", "url": "https://example.gov/results"}
    ]
    await database.close()
