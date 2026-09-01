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
    other_item = FeedItem(
        "two",
        "Legislature schedules a related hearing",
        "https://example.com/b",
        "A hearing will examine the election process",
        "Wire",
        datetime.now(UTC),
    )
    other_normalized = normalize_item(other_item, fallback_publisher=feed.name)
    other_story, _ = await database.create_story_with_article(
        feed=feed,
        item=other_item,
        normalized=other_normalized,
        tags=("Politics",),
        state=StoryState.NEW,
        priority=10,
    )
    await database.mark_story_published(story.id, thread_id=101, message_id=201)
    await database.mark_story_published(other_story.id, thread_id=102, message_id=202)
    await database.save_story_analysis(
        story.id,
        analysis="**Summary**\nVotes are being counted.\n\n**Key facts**\n- Counting continues.",
        related_story_ids=(other_story.id,),
    )
    await database.save_story_analysis(
        story.id,
        analysis="**Summary**\nVotes are being counted.\n\n**Key facts**\n- Counting continues.",
        related_story_ids=(other_story.id,),
    )
    assert [item.id for item in await database.related_stories(story.id)] == [other_story.id]
    assert [item.id for item in await database.related_stories(other_story.id)] == [story.id]
    await database.close()

    reopened = Database(path)
    await reopened.connect()
    assert (await reopened.get_story(story.id)).title == story.title  # type: ignore[union-attr]
    assert (await reopened.get_article(article.id)).canonical_url == "https://example.com/a"  # type: ignore[union-attr]
    assert (await reopened.get_feed(feed.id)).default_tags == ("World",)  # type: ignore[union-attr]
    reopened_story = await reopened.get_story(story.id)
    assert reopened_story is not None
    assert reopened_story.analysis_state.value == "ready"
    assert (
        reopened_story.analysis is not None and "Votes are being counted" in reopened_story.analysis
    )
    assert [item.id for item in await reopened.related_stories(story.id)] == [other_story.id]
    await reopened.close()
