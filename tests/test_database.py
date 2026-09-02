from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigbot.database import Database, DuplicateFeedError
from bigbot.domain import FeedItem, FeedKind, FeedState


async def _feed(database: Database):
    return await database.add_feed(
        guild_id=1,
        forum_channel_id=2,
        name="news",
        kind=FeedKind.RSS,
        source="https://example.com/rss",
        interval_seconds=300,
        tag_ids=(3,),
        include_replies=False,
        include_reposts=False,
        created_by=4,
    )


async def test_feed_lifecycle_and_delivery_dedup(tmp_path) -> None:
    database = Database(tmp_path / "big.db")
    await database.connect()
    feed = await _feed(database)
    assert feed.name == "news"
    assert feed.tag_ids == (3,)
    assert len(await database.due_feeds(datetime.now(UTC))) == 1

    await database.record_fetch_error(
        feed.id,
        next_poll_at=datetime.now(UTC) + timedelta(minutes=10),
        error="temporary failure",
    )
    failed = await database.get_feed(feed.id)
    assert failed is not None
    assert failed.last_polled_at is None
    assert failed.last_error == "temporary failure"

    item = FeedItem("entry-1", "Title", "https://example.com/1", "Body", None, None)
    assert await database.claim_delivery(feed.id, item)
    assert not await database.claim_delivery(feed.id, item)
    await database.finish_delivery(feed.id, item.external_id, thread_id=10, message_id=11)
    assert (await database.delivery_counts(feed.id))["posted"] == 1

    assert await database.set_feed_state(feed.id, FeedState.PAUSED)
    assert (await database.get_feed(feed.id)).state is FeedState.PAUSED  # type: ignore[union-attr]
    assert await database.remove_feed(feed.id)
    assert await database.get_feed(feed.id) is None
    await database.close()


async def test_duplicate_name_is_scoped_to_guild(tmp_path) -> None:
    database = Database(tmp_path / "big.db")
    await database.connect()
    await _feed(database)
    with pytest.raises(DuplicateFeedError):
        await _feed(database)
    await database.close()


async def test_openrouter_model_setting_persists_by_guild(tmp_path) -> None:
    path = tmp_path / "big.db"
    database = Database(path)
    await database.connect()
    await database.set_openrouter_model(
        guild_id=11,
        model="deepseek/deepseek-v4-flash-0731",
        actor_id=22,
    )
    await database.close()

    reopened = Database(path)
    await reopened.connect()
    assert await reopened.openrouter_models() == {11: "deepseek/deepseek-v4-flash-0731"}
    await reopened.close()
