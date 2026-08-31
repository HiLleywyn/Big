from __future__ import annotations

from datetime import UTC, datetime

from bigbot.database import Database
from bigbot.domain import FeedItem, FeedKind, StoryState
from bigbot.normalization import normalize_item


async def test_story_schema_migrates_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "big.db"
    database = Database(path)
    await database.connect()
    feed = await database.add_feed(
        guild_id=1,
        forum_channel_id=2,
        name="news",
        kind=FeedKind.RSS,
        source="https://example.com/rss",
        interval_seconds=300,
        tag_ids=(),
        default_tags=("World",),
        include_replies=False,
        include_reposts=False,
        created_by=3,
    )
    item = FeedItem(
        "one",
        "Country holds election",
        "https://example.com/a",
        "Votes are counted",
        "Wire",
        datetime.now(UTC),
    )
    normalized = normalize_item(item, fallback_publisher=feed.name)
    story, article = await database.create_story_with_article(
        feed=feed,
        item=item,
        normalized=normalized,
        tags=("Politics",),
        state=StoryState.NEW,
        priority=10,
    )
    await database.close()

    reopened = Database(path)
    await reopened.connect()
    assert (await reopened.get_story(story.id)).title == story.title  # type: ignore[union-attr]
    assert (await reopened.get_article(article.id)).canonical_url == "https://example.com/a"  # type: ignore[union-attr]
    assert (await reopened.get_feed(feed.id)).default_tags == ("World",)  # type: ignore[union-attr]
    await reopened.close()
