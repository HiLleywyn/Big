from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bigbot.classification import StoryClassifier
from bigbot.clustering import DeterministicClusterer
from bigbot.database import Database
from bigbot.domain import (
    Article,
    Feed,
    FeedItem,
    FeedKind,
    FetchResult,
    PublishReceipt,
    Story,
    StoryState,
)
from bigbot.enrichment import EnrichmentError, StoryAnalysis
from bigbot.normalization import normalize_item
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
        self.archived = 0
        self.deleted = 0
        self.created_story_ids: list[int] = []
        self.updated_story_ids: list[int] = []
        self.related_by_story: dict[int, tuple[int, ...]] = {}
        self.analysis_by_story: dict[int, str | None] = {}

    async def create_story(
        self,
        feed: Feed,
        story: Story,
        articles: list[Article],
        related_stories: list[Story],
    ) -> PublishReceipt:
        self.created += 1
        self.created_story_ids.append(story.id)
        self.related_by_story[story.id] = tuple(item.id for item in related_stories)
        self.analysis_by_story[story.id] = story.analysis
        if self.uncertain:
            raise PublishError("unknown outcome", uncertain=True)
        return PublishReceipt(100 + self.created, 100 + self.created)

    async def update_story(
        self,
        story: Story,
        articles: list[Article],
        article: Article,
        related_stories: list[Story],
        *,
        post_update: bool,
    ) -> int | None:
        self.updated += 1
        self.updated_story_ids.append(story.id)
        self.related_by_story[story.id] = tuple(item.id for item in related_stories)
        self.analysis_by_story[story.id] = story.analysis
        return 500 + self.updated if post_update else None

    async def mark_merged(self, source: Story, target: Story) -> None:
        self.merged += 1

    async def archive_story(self, story: Story) -> None:
        self.archived += 1

    async def delete_story(self, story: Story) -> None:
        self.deleted += 1


class FakeAnalyzer:
    def __init__(self, *, fail: bool = False, relate_first: bool = False) -> None:
        self.fail = fail
        self.relate_first = relate_first
        self.calls: list[tuple[str, ...]] = []
        self.closed = False
        self.models: dict[int, str] = {}

    async def analyze_story(
        self,
        story: Story,
        articles: list[Article],
        relationship_candidates: list[Story],
    ) -> StoryAnalysis:
        self.calls.append(tuple(article.publisher for article in articles))
        if self.fail:
            raise EnrichmentError("OpenRouter unavailable")
        related = (
            (relationship_candidates[0].id,)
            if self.relate_first and relationship_candidates
            else ()
        )
        return StoryAnalysis(
            text=(
                "**Summary**\n"
                f"Analysis from {len(articles)} sources.\n\n"
                "**Key facts**\n"
                "- Available reports describe the event."
            ),
            related_story_ids=related,
        )

    async def close(self) -> None:
        self.closed = True

    def model_for(self, guild_id: int) -> str:
        return self.models.get(guild_id, "openrouter/auto")

    async def validate_model(self, model: str) -> str:
        if model != "deepseek/deepseek-v4-flash-0731":
            raise ValueError("OpenRouter model was not found")
        return model

    def set_model(self, guild_id: int, model: str) -> None:
        self.models[guild_id] = model


async def _feed(
    database: Database,
    name: str = "wire",
    *,
    kind: FeedKind = FeedKind.RSS,
    summarization_enabled: bool = True,
) -> Feed:
    return await database.add_feed(
        guild_id=1,
        forum_channel_id=2,
        name=name,
        kind=kind,
        source=f"https://example.com/{name}.rss",
        interval_seconds=300,
        tag_ids=(),
        default_tags=("Markets",),
        include_replies=False,
        include_reposts=False,
        created_by=3,
        summarization_enabled=summarization_enabled,
    )


def _item(external_id: str, title: str, publisher: str, url: str) -> FeedItem:
    return FeedItem(
        external_id, title, url, title, publisher, datetime.now(UTC), publisher=publisher
    )


async def _service(
    path: Path,
    publisher: FakePublisher,
    source: FakeSource,
    *,
    analyzer: FakeAnalyzer | None = None,
    sources: dict[FeedKind, FakeSource] | None = None,
) -> tuple[Database, FeedService]:
    database = Database(path)
    await database.connect()
    service = FeedService(
        database=database,
        sources=sources or {FeedKind.RSS: source},
        publisher=publisher,
        clusterer=DeterministicClusterer(),
        classifier=StoryClassifier.with_defaults(),
        tick_seconds=15,
        max_backfill=2,
        clustering_window_hours=72,
        stale_after_hours=96,
        analyzer=analyzer,
    )
    return database, service


async def test_multiple_publishers_become_one_forum_story(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer()
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
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
    assert analyzer.calls == [("Reuters",), ("Reuters", "AP")]
    stories = await database.candidate_stories(
        guild_id=1, forum_channel_id=2, since=datetime(2000, 1, 1, tzinfo=UTC)
    )
    assert len(stories) == 1
    assert len(await database.story_articles(stories[0].id)) == 2
    stored = await database.get_story(stories[0].id)
    assert stored is not None
    assert stored.analysis is not None and "2 sources" in stored.analysis
    assert publisher.updated_story_ids == [stored.id]
    assert "2 sources" in (publisher.analysis_by_story[stored.id] or "")
    await database.close()


async def test_feed_summary_setting_skips_model_and_uses_deterministic_story(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer()
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
    feed = await _feed(database, summarization_enabled=False)

    assert (
        await service.process_item(
            feed,
            _item("one", "Court publishes a final ruling", "Wire", "https://example.com/one"),
        )
        == "new_stories"
    )

    story = (await database.published_stories(limit=1))[0]
    assert analyzer.calls == []
    assert story.analysis is None
    assert story.analysis_state.value == "disabled"
    assert publisher.analysis_by_story[story.id] is None
    await service.close()
    await database.close()


async def test_one_enabled_feed_summarizes_complete_multi_source_story(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer()
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
    disabled = await _feed(database, "wire", summarization_enabled=False)
    enabled = await _feed(database, "wire-two", summarization_enabled=True)

    first = _item(
        "one",
        "Federal Reserve cuts interest rates by 25 basis points",
        "Reuters",
        "https://reuters.example/rates",
    )
    second = _item(
        "two",
        "Fed lowers key interest rate by quarter point",
        "AP",
        "https://ap.example/rates",
    )
    assert await service.process_item(disabled, first) == "new_stories"
    assert await service.process_item(enabled, second) == "updated_stories"
    assert analyzer.calls == [("Reuters", "AP")]
    story = (await database.published_stories(limit=1))[0]
    assert story.analysis is not None and "2 sources" in story.analysis
    await service.close()
    await database.close()


async def test_external_item_uses_process_item_and_resolves_duplicate_story(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer()
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
    feed = await _feed(database)
    item = _item(
        "manual:one",
        "Central bank announces a rate decision",
        "Example News",
        "https://news.example/rates?utm_source=discord",
    )
    first = await service.process_external_item(feed, item)
    duplicate = await service.process_external_item(
        feed,
        _item(
            "manual:two",
            item.title,
            item.publisher or "Example News",
            "https://news.example/rates",
        ),
    )
    assert first.outcome == "new_stories"
    assert duplicate.outcome == "duplicates"
    assert duplicate.story.id == first.story.id
    assert duplicate.article.id == first.article.id
    assert publisher.created == 1
    assert publisher.updated == 0
    assert analyzer.calls == [("Example News",)]
    await service.close()
    await database.close()


async def test_presentation_refresh_reclassifies_and_edits_existing_story(tmp_path) -> None:
    publisher = FakePublisher()
    database, service = await _service(tmp_path / "big.db", publisher, FakeSource(()))
    feed = await _feed(database)
    outcome = await service.process_item(
        feed,
        _item(
            "fed-1",
            "Federal Reserve cuts interest rate after inflation report",
            "Wire",
            "https://example.com/fed-1",
        ),
    )
    assert outcome == "new_stories"

    updated, failed = await service.refresh_presentation(force=True)
    assert (updated, failed) == (1, 0)
    assert publisher.updated == 1
    story = (await database.published_stories(limit=1))[0]
    assert story.tags[:2] == ("Markets", "Economy")

    await service.close()
    await database.close()


async def test_startup_analysis_recovery_uses_finalization_without_duplicate_post(tmp_path) -> None:
    publisher = FakePublisher()
    database, service = await _service(tmp_path / "big.db", publisher, FakeSource(()))
    feed = await _feed(database)
    assert (
        await service.process_item(
            feed,
            _item("story-1", "Court publishes a final ruling", "Wire", "https://example.com/1"),
        )
        == "new_stories"
    )
    assert publisher.created == 1

    analyzer = FakeAnalyzer()
    recovered = FeedService(
        database=database,
        sources={FeedKind.RSS: FakeSource(())},
        publisher=publisher,
        clusterer=DeterministicClusterer(),
        classifier=StoryClassifier.with_defaults(),
        tick_seconds=15,
        max_backfill=2,
        clustering_window_hours=72,
        stale_after_hours=96,
        analyzer=analyzer,
    )
    assert await recovered.recover_story_analysis() == (1, 0)
    assert publisher.created == 1
    assert publisher.updated == 1
    story = (await database.published_stories(limit=1))[0]
    assert story.analysis_state.value == "ready"

    await service.close()
    await recovered.close()
    await database.close()


async def test_pending_story_recovery_finishes_confirmed_pre_publish_state(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer()
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
    feed = await _feed(database)
    item = _item("pending-1", "Court publishes a final ruling", "Wire", "https://example.com/1")
    normalized = normalize_item(item, fallback_publisher=feed.publisher)
    story, _ = await database.create_story_with_article(
        feed=feed,
        item=item,
        normalized=normalized,
        tags=("Law",),
        state=StoryState.NEW,
        priority=0,
    )

    assert await service.recover_pending_stories() == (1, 0)
    recovered = await database.get_story(story.id)
    assert recovered is not None
    assert recovered.publication_state.value == "published"
    assert recovered.analysis_state.value == "ready"
    assert publisher.created == 1

    await service.close()
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


async def test_retention_archives_old_forum_stories_once(tmp_path) -> None:
    publisher = FakePublisher()
    database, service = await _service(tmp_path / "big.db", publisher, FakeSource(()))
    service._retention_after = timedelta(days=1)
    service._retention_action = "archive"
    feed = await _feed(database)
    old_item = FeedItem(
        "old",
        "Old market story",
        "https://news.example/old",
        "Old market story",
        "Wire",
        datetime.now(UTC) - timedelta(days=3),
    )
    assert await service.process_item(feed, old_item) == "new_stories"
    story = (
        await database.candidate_stories(
            guild_id=1, forum_channel_id=2, since=datetime(2000, 1, 1, tzinfo=UTC)
        )
    )[0]
    old_timestamp = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    await database._db().execute(
        "UPDATE stories SET last_updated_at = ?, updated_at = ? WHERE id = ?",
        (old_timestamp, old_timestamp, story.id),
    )
    await database._db().commit()
    assert await service.cleanup_old_stories() == 1
    assert await service.cleanup_old_stories() == 0
    assert publisher.archived == 1
    assert publisher.deleted == 0
    cursor = await database._db().execute(
        "SELECT clear_action FROM stories WHERE id = ?",
        (story.id,),
    )
    row = await cursor.fetchone()
    assert row["clear_action"] == "archive"
    await database.close()


async def test_direct_story_relationships_are_reciprocal_in_discord(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer(relate_first=True)
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
    feed = await _feed(database)
    first = _item(
        "one",
        "Central bank announces emergency lending program",
        "Wire",
        "https://news.example/lending",
    )
    second = _item(
        "two",
        "Treasury responds to emergency lending announcement",
        "Wire",
        "https://news.example/treasury-response",
    )
    assert await service.process_item(feed, first) == "new_stories"
    assert await service.process_item(feed, second) == "new_stories"
    assert publisher.created == 2
    first_id, second_id = publisher.created_story_ids
    assert publisher.related_by_story[second_id] == (first_id,)
    assert publisher.related_by_story[first_id] == (second_id,)
    assert [story.id for story in await database.related_stories(first_id)] == [second_id]
    assert [story.id for story in await database.related_stories(second_id)] == [first_id]
    await database.close()


async def test_openrouter_failure_uses_same_finalizer_without_duplicate_post(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer(fail=True)
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
    feed = await _feed(database)
    item = _item("one", "A confirmed event occurred", "Wire", "https://news.example/event")
    assert await service.process_item(feed, item) == "new_stories"
    story = (
        await database.candidate_stories(
            guild_id=1, forum_channel_id=2, since=datetime(2000, 1, 1, tzinfo=UTC)
        )
    )[0]
    article = (await database.story_articles(story.id))[0]
    assert story.analysis_state.value == "failed"
    assert story.analysis_error == "OpenRouter unavailable"
    assert publisher.created == 1
    assert await service.reprocess_article(article.id) == "updated_stories"
    assert publisher.created == 1
    assert publisher.updated == 1
    assert len(analyzer.calls) == 2
    await database.close()


async def test_admin_model_setting_is_validated_persisted_and_applied(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer()
    database, service = await _service(
        tmp_path / "big.db", publisher, FakeSource(()), analyzer=analyzer
    )
    model = await service.configure_analysis_model(
        guild_id=1,
        model="deepseek/deepseek-v4-flash-0731",
        actor_id=42,
    )
    assert model == "deepseek/deepseek-v4-flash-0731"
    assert service.analysis_model(1) == model
    assert await database.openrouter_models() == {1: model}
    await database.close()


async def test_rss_and_x_feeds_share_process_item_pipeline(tmp_path) -> None:
    publisher = FakePublisher()
    analyzer = FakeAnalyzer()
    rss_source = FakeSource(
        (_item("rss-1", "Port authority changes cargo rules", "RSS Wire", "https://rss.example/1"),)
    )
    x_source = FakeSource(
        (
            _item(
                "x-1",
                "Space agency schedules a new launch",
                "X Account",
                "https://x.com/example/status/1",
            ),
        )
    )
    database, service = await _service(
        tmp_path / "big.db",
        publisher,
        rss_source,
        analyzer=analyzer,
        sources={FeedKind.RSS: rss_source, FeedKind.X: x_source},
    )
    rss = await _feed(database, "rss", kind=FeedKind.RSS)
    x_feed = await _feed(database, "x", kind=FeedKind.X)
    assert (await service.poll_feed(rss.id)).new_stories == 1
    assert (await service.poll_feed(x_feed.id)).new_stories == 1
    assert analyzer.calls == [("RSS Wire",), ("X Account",)]
    assert publisher.created == 2
    await service.close()
    assert analyzer.closed is True
    await database.close()
