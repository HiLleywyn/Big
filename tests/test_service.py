from __future__ import annotations

from pathlib import Path

from bigbot.database import Database
from bigbot.domain import Feed, FeedItem, FeedKind, FetchResult, PublishReceipt
from bigbot.publisher import PublishError
from bigbot.service import FeedService


class FakeSource:
    def __init__(self, items: tuple[FeedItem, ...]) -> None:
        self.items = items

    async def fetch(self, feed: Feed) -> FetchResult:
        return FetchResult(self.items, cursor="99")

    async def close(self) -> None:
        pass


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def publish(self, feed: Feed, item: FeedItem) -> PublishReceipt:
        self.calls += 1
        if self.fail:
            raise PublishError("unknown outcome")
        return PublishReceipt(thread_id=100 + self.calls, message_id=200 + self.calls)

    async def reply(self, thread_id: int, content: str) -> int:
        return 300 + self.calls


async def create_feed(database: Database) -> Feed:
    return await database.add_feed(
        guild_id=1,
        forum_channel_id=2,
        name="wire",
        kind=FeedKind.RSS,
        source="https://example.com/rss",
        interval_seconds=300,
        tag_ids=(),
        include_replies=False,
        include_reposts=False,
        created_by=3,
    )


def item(number: int) -> FeedItem:
    return FeedItem(
        str(number), f"Topic {number}", f"https://example.com/{number}", "Summary", None, None
    )


async def service(path: Path, publisher: FakePublisher, items: tuple[FeedItem, ...]):
    database = Database(path)
    await database.connect()
    feed = await create_feed(database)
    source = FakeSource(items)
    instance = FeedService(
        database=database,
        sources={FeedKind.RSS: source},
        publisher=publisher,
        enricher=None,
        tick_seconds=15,
        max_backfill=2,
    )
    return database, feed, instance


async def test_first_poll_backfill_and_restart_dedup(tmp_path) -> None:
    publisher = FakePublisher()
    database, feed, instance = await service(
        tmp_path / "big.db", publisher, (item(1), item(2), item(3))
    )
    first = await instance.poll_feed(feed.id)
    second = await instance.poll_feed(feed.id)
    assert first.posted == 2
    assert second.posted == 0
    assert second.skipped == 3
    assert (await database.delivery_counts(feed.id))["skipped"] == 1
    await database.close()


async def test_uncertain_publish_is_never_retried(tmp_path) -> None:
    publisher = FakePublisher(fail=True)
    database, feed, instance = await service(tmp_path / "big.db", publisher, (item(1),))
    first = await instance.poll_feed(feed.id)
    second = await instance.poll_feed(feed.id)
    assert first.uncertain == 1
    assert second.skipped == 1
    assert publisher.calls == 1
    assert (await database.delivery_counts(feed.id))["uncertain"] == 1
    await database.close()
