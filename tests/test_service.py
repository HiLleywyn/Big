from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bigbot.classification import StoryClassifier
from bigbot.clustering import DeterministicClusterer
from bigbot.database import Database
from bigbot.domain import Article, Feed, FeedItem, FeedKind, FetchResult, PublishReceipt, Story
from bigbot.publisher import PublishError
from bigbot.service import FeedService


class FakeSource:
    def __init__(self, items: tuple[FeedItem, ...]) -> None:
        self.items = items

    async def fetch(self, feed: Feed) -> FetchResult:
        return FetchResult(self.items)

    async def close(self) -> None:
        pass


class FakePublisher:
    def __init__(self, *, uncertain: bool = False) -> None:
        self.uncertain = uncertain
        self.created = 0
        self.updated = 0
        self.merged = 0

    async def create_story(
        self, feed: Feed, story: Story, articles: list[Article]
    ) -> PublishReceipt:
        self.created += 1
        if self.uncertain:
            raise PublishError("unknown outcome", uncertain=True)
        return PublishReceipt(100 + self.created, 100 + self.created)

    async def update_story(
        self,
        story: Story,
        articles: list[Article],
        article: Article,
        *,
        post_update: bool,
    ) -> int | None:
        self.updated += 1
        return 500 + self.updated if post_update else None

    async def mark_merged(self, source: Story, target: Story) -> None:
        self.merged += 1


async def _feed(database: Database, name: str = "wire") -> Feed:
    return await database.add_feed(
        guild_id=1,
        forum_channel_id=2,
        name=name,
        kind=FeedKind.RSS,
        source=f"https://example.com/{name}.rss",
        interval_seconds=300,
        tag_ids=(),
        default_tags=("Markets",),
        include_replies=False,
        include_reposts=False,
        created_by=3,
    )


def _item(external_id: str, title: str, publisher: str, url: str) -> FeedItem:
    return FeedItem(
        external_id, title, url, title, publisher, datetime.now(UTC), publisher=publisher
    )


async def _service(
    path: Path, publisher: FakePublisher, source: FakeSource
) -> tuple[Database, FeedService]:
    database = Database(path)
    await database.connect()
    service = FeedService(
        database=database,
        sources={FeedKind.RSS: source},
        publisher=publisher,
        clusterer=DeterministicClusterer(),
        classifier=StoryClassifier.with_defaults(),
        tick_seconds=15,
        max_backfill=2,
        clustering_window_hours=72,
        stale_after_hours=96,
    )
    return database, service


async def test_multiple_publishers_become_one_forum_story(tmp_path) -> None:
    publisher = FakePublisher()
    database, service = await _service(tmp_path / "big.db", publisher, FakeSource(()))
    reuters = await _feed(database, "reuters")
    ap = await _feed(database, "ap")
    first = _item(
        "r1",
        "Federal Reserve cuts interest rates by 25 basis points",
        "Reuters",
        "https://reuters.example/rates",
    )
    second = _item(
        "a1",
        "Fed lowers key interest rate by quarter point",
        "AP",
        "https://ap.example/fed-rate-cut",
    )
    assert await service.process_item(reuters, first) == "new_stories"
    assert await service.process_item(ap, second) == "updated_stories"
    assert publisher.created == 1
    assert publisher.updated == 1
    stories = await database.candidate_stories(
        guild_id=1, forum_channel_id=2, since=datetime(2000, 1, 1, tzinfo=UTC)
    )
    assert len(stories) == 1
    assert len(await database.story_articles(stories[0].id)) == 2
    await database.close()


async def test_tracking_url_duplicates_are_not_reposted(tmp_path) -> None:
    publisher = FakePublisher()
    database, service = await _service(tmp_path / "big.db", publisher, FakeSource(()))
    feed = await _feed(database)
    first = _item("one", "A real event happened", "Wire", "https://news.example/a?utm_source=x")
    duplicate = _item("two", "A real event happened", "Wire", "https://news.example/a")
    assert await service.process_item(feed, first) == "new_stories"
    assert await service.process_item(feed, duplicate) == "duplicates"
    assert publisher.created == 1
    await database.close()


async def test_uncertain_publish_is_never_retried(tmp_path) -> None:
    publisher = FakePublisher(uncertain=True)
    item = _item("one", "A real event happened", "Wire", "https://news.example/a")
    source = FakeSource((item,))
    database, service = await _service(tmp_path / "big.db", publisher, source)
    feed = await _feed(database)
    first = await service.poll_feed(feed.id)
    second = await service.poll_feed(feed.id)
    assert first.uncertain == 1
    assert second.duplicates == 1
    assert publisher.created == 1
    await database.close()


async def test_initial_backfill_is_bounded_and_persisted(tmp_path) -> None:
    items = tuple(
        _item(
            str(index), f"Unrelated event number {index}", "Wire", f"https://news.example/{index}"
        )
        for index in range(4)
    )
    publisher = FakePublisher()
    database, service = await _service(tmp_path / "big.db", publisher, FakeSource(items))
    feed = await _feed(database)
    report = await service.poll_feed(feed.id)
    assert report.skipped == 2
    assert report.new_stories == 2
    await database.close()


async def test_moderator_can_merge_then_split_story_sources(tmp_path) -> None:
    publisher = FakePublisher()
    database, service = await _service(tmp_path / "big.db", publisher, FakeSource(()))
    feed = await _feed(database)
    first = _item("one", "Central bank cuts rates", "Wire", "https://news.example/one")
    second = _item("two", "Space company launches rocket", "Wire", "https://news.example/two")
    assert await service.process_item(feed, first) == "new_stories"
    assert await service.process_item(feed, second) == "new_stories"
    stories = await database.candidate_stories(
        guild_id=1, forum_channel_id=2, since=datetime(2000, 1, 1, tzinfo=UTC)
    )
    target = next(story for story in stories if "bank" in story.title.casefold())
    source = next(story for story in stories if "rocket" in story.title.casefold())

    merged = await service.merge_stories(target.id, source.id, actor_id=99)
    assert merged.id == target.id
    assert publisher.merged == 1
    combined = await database.story_articles(target.id)
    assert len(combined) == 2

    split = await service.split_article(combined[-1].id, actor_id=99)
    assert split.id not in {target.id, source.id}
    assert len(await database.story_articles(split.id)) == 1
    assert len(await database.story_articles(target.id)) == 1
    await database.close()
