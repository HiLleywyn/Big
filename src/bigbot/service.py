from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from bigbot.classification import StoryClassifier
from bigbot.clustering import StoryClusterer
from bigbot.database import Database
from bigbot.domain import (
    Article,
    DeliveryState,
    Feed,
    FeedItem,
    FeedKind,
    PublicationState,
    Story,
    StoryState,
    utc_now,
)
from bigbot.feeds.base import FeedFetchError, FeedSource
from bigbot.normalization import NormalizedArticle, normalize_item
from bigbot.publisher import ForumPublisher, PublishError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollReport:
    fetched: int = 0
    new_stories: int = 0
    updated_stories: int = 0
    duplicates: int = 0
    skipped: int = 0
    failed: int = 0
    uncertain: int = 0

    @property
    def posted(self) -> int:
        return self.new_stories + self.updated_stories


class FeedService:
    def __init__(
        self,
        *,
        database: Database,
        sources: dict[FeedKind, FeedSource],
        publisher: ForumPublisher,
        clusterer: StoryClusterer,
        classifier: StoryClassifier,
        tick_seconds: int,
        max_backfill: int,
        clustering_window_hours: int,
        stale_after_hours: int,
        source_priorities: dict[str, int] | None = None,
        post_major_updates: bool = True,
        post_source_updates: bool = False,
        retention_after_days: int | None = None,
        retention_action: str = "archive",
        retention_batch_size: int = 25,
    ) -> None:
        self._database = database
        self._sources = sources
        self._publisher = publisher
        self._clusterer = clusterer
        self._classifier = classifier
        self._tick_seconds = tick_seconds
        self._max_backfill = max_backfill
        self._window = timedelta(hours=clustering_window_hours)
        self._stale_after = timedelta(hours=stale_after_hours)
        self._priorities = {
            key.casefold(): value for key, value in (source_priorities or {}).items()
        }
        self._post_major_updates = post_major_updates
        self._post_source_updates = post_source_updates
        self._retention_after = (
            timedelta(days=retention_after_days) if retention_after_days is not None else None
        )
        self._retention_action = retention_action
        self._retention_batch_size = retention_batch_size
        self._feed_locks: dict[int, asyncio.Lock] = {}
        self._cluster_lock = asyncio.Lock()
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._database.mark_stale_stories(utc_now() - self._stale_after)
                await self.cleanup_old_stories()
                for feed in await self._database.due_feeds(utc_now()):
                    try:
                        await self.poll_feed(feed.id)
                    except Exception:
                        log.exception(
                            "feed poll crashed",
                            extra={"event": "poll_crash", "feed_id": feed.id},
                        )
            except Exception:
                log.exception("scheduler tick failed", extra={"event": "scheduler_crash"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick_seconds)

    async def cleanup_old_stories(self) -> int:
        if self._retention_after is None:
            return 0
        stories = await self._database.stories_for_cleanup(
            before=utc_now() - self._retention_after,
            limit=self._retention_batch_size,
        )
        cleared = 0
        for story in stories:
            try:
                if self._retention_action == "delete":
                    await self._publisher.delete_story(story)
                else:
                    await self._publisher.archive_story(story)
                await self._database.mark_story_cleared(story.id, action=self._retention_action)
                cleared += 1
            except PublishError:
                log.exception(
                    "story cleanup failed",
                    extra={
                        "event": "cleanup_failed",
                        "story_id": story.id,
                        "action": self._retention_action,
                    },
                )
        if cleared:
            log.info(
                "old stories cleared",
                extra={
                    "event": "cleanup_completed",
                    "action": self._retention_action,
                    "count": cleared,
                },
            )
        return cleared

    def stop(self) -> None:
        self._stopping.set()

    async def close(self) -> None:
        self.stop()
        for source in self._sources.values():
            await source.close()

    async def poll_feed(self, feed_id: int) -> PollReport:
        lock = self._feed_locks.setdefault(feed_id, asyncio.Lock())
        async with lock:
            feed = await self._database.get_feed(feed_id)
            if feed is None:
                raise ValueError(f"feed {feed_id} does not exist")
            source = self._sources[feed.kind]
            try:
                result = await source.fetch(feed)
            except (FeedFetchError, ValueError) as exc:
                multiplier = 2 ** min(feed.failure_count + 1, 8)
                delay = min(feed.interval_seconds * multiplier, 86400)
                await self._database.record_fetch_error(
                    feed.id, next_poll_at=utc_now() + timedelta(seconds=delay), error=str(exc)
                )
                log.warning(
                    "feed fetch failed",
                    extra={"event": "fetch_failed", "feed_id": feed.id},
                )
                raise

            next_poll = utc_now() + timedelta(seconds=feed.interval_seconds)
            items = list(result.items)
            suppressed: list[FeedItem] = []
            if feed.last_polled_at is None and len(items) > self._max_backfill:
                suppressed = items[: -self._max_backfill]
                items = items[-self._max_backfill :]

            counts = {
                "new_stories": 0,
                "updated_stories": 0,
                "duplicates": 0,
                "skipped": 0,
                "failed": 0,
                "uncertain": 0,
            }
            for item in suppressed:
                normalized = normalize_item(item, fallback_publisher=feed.publisher or feed.name)
                if await self._duplicate(feed, item, normalized):
                    counts["duplicates"] += 1
                else:
                    await self._database.record_skipped_article(
                        feed=feed, item=item, normalized=normalized
                    )
                    counts["skipped"] += 1

            for item in items:
                outcome = await self.process_item(feed, item)
                counts[outcome] += 1

            error = None
            if counts["uncertain"]:
                error = f"{counts['uncertain']} Discord outcome(s) uncertain"
            await self._database.update_after_fetch(
                feed.id,
                next_poll_at=next_poll,
                cursor=result.cursor if result.cursor is not None else feed.cursor,
                etag=result.etag if result.etag is not None else feed.etag,
                last_modified=(
                    result.last_modified if result.last_modified is not None else feed.last_modified
                ),
                error=error,
            )
            return PollReport(fetched=len(result.items), **counts)

    async def process_item(self, feed: Feed, item: FeedItem) -> str:
        normalized = normalize_item(item, fallback_publisher=feed.publisher or feed.name)
        async with self._cluster_lock:
            if await self._duplicate(feed, item, normalized):
                return "duplicates"
            published = item.published_at or utc_now()
            candidates = await self._database.candidate_stories(
                guild_id=feed.guild_id,
                forum_channel_id=feed.forum_channel_id,
                since=published - self._window,
            )
            decision = self._clusterer.select(normalized, published, candidates)
            tags = self._classifier.classify(normalized, feed_tags=feed.default_tags)
            priority = self._priority(normalized.publisher, feed.publisher or feed.name)
            if decision.story is None:
                state = (
                    StoryState.BREAKING
                    if any(tag.casefold() == "breaking" for tag in tags)
                    else StoryState.NEW
                )
                story, article = await self._database.create_story_with_article(
                    feed=feed,
                    item=item,
                    normalized=normalized,
                    tags=tags,
                    state=state,
                    priority=priority,
                )
                return await self._publish_new(feed, story, article)

            old_story = decision.story
            if old_story.state is StoryState.BREAKING:
                state = StoryState.BREAKING
            elif decision.significant_update:
                state = StoryState.DEVELOPING
            else:
                state = StoryState.UPDATED
            story, article = await self._database.attach_article(
                story=old_story,
                feed=feed,
                item=item,
                normalized=normalized,
                tags=tags,
                state=state,
                priority=priority,
                significant=decision.significant_update,
            )
            log.info(
                "article clustered",
                extra={
                    "event": "article_clustered",
                    "feed_id": feed.id,
                    "story_id": story.id,
                    "article_id": article.id,
                    "cluster_score": round(decision.score, 4),
                },
            )
            return await self._publish_update(story, article, decision.significant_update)

    async def _publish_new(self, feed: Feed, story: Story, article: Article) -> str:
        try:
            receipt = await self._publisher.create_story(feed, story, [article])
        except PublishError as exc:
            state = PublicationState.UNCERTAIN if exc.uncertain else PublicationState.FAILED
            delivery = DeliveryState.UNCERTAIN if exc.uncertain else DeliveryState.FAILED
            await self._database.mark_story_publication(story.id, state)
            await self._database.mark_article_delivery(article.id, delivery, error=str(exc))
            return "uncertain" if exc.uncertain else "failed"
        await self._database.mark_story_published(
            story.id, thread_id=receipt.thread_id, message_id=receipt.message_id
        )
        await self._database.mark_article_delivery(article.id, DeliveryState.POSTED)
        log.info(
            "new story published",
            extra={"event": "story_created", "story_id": story.id, "article_id": article.id},
        )
        return "new_stories"

    async def _publish_update(self, story: Story, article: Article, significant: bool) -> str:
        articles = await self._database.story_articles(story.id)
        post_update = self._post_source_updates or (significant and self._post_major_updates)
        try:
            message_id = await self._publisher.update_story(
                story, articles, article, post_update=post_update
            )
        except PublishError as exc:
            state = DeliveryState.UNCERTAIN if exc.uncertain else DeliveryState.FAILED
            await self._database.mark_article_delivery(article.id, state, error=str(exc))
            return "uncertain" if exc.uncertain else "failed"
        await self._database.mark_article_delivery(
            article.id, DeliveryState.POSTED, update_message_id=message_id
        )
        return "updated_stories"

    async def _duplicate(
        self, feed: Feed, item: FeedItem, normalized: NormalizedArticle
    ) -> Article | None:
        return await self._database.find_duplicate_article(
            feed_id=feed.id,
            external_id=item.external_id,
            canonical_url=normalized.canonical_url,
            publisher=normalized.publisher,
            fingerprint=normalized.fingerprint,
        )

    def _priority(self, publisher: str, feed_name: str) -> int:
        return self._priorities.get(
            publisher.casefold(), self._priorities.get(feed_name.casefold(), 0)
        )

    async def merge_stories(self, target_id: int, source_id: int, *, actor_id: int) -> Story:
        target, source = await self._database.merge_stories(target_id, source_id, actor_id=actor_id)
        articles = await self._database.story_articles(target.id)
        if articles:
            await self._publisher.update_story(target, articles, articles[-1], post_update=False)
        await self._publisher.mark_merged(source, target)
        return target

    async def split_article(self, article_id: int, *, actor_id: int) -> Story:
        original, story, article = await self._database.split_article(article_id, actor_id=actor_id)
        if article.feed_id is None:
            raise ValueError("article feed no longer exists")
        feed = await self._database.get_feed(article.feed_id)
        if feed is None:
            raise ValueError("article feed no longer exists")
        await self._database.mark_article_delivery(article.id, DeliveryState.PENDING)
        receipt = await self._publisher.create_story(feed, story, [article])
        await self._database.mark_story_published(
            story.id, thread_id=receipt.thread_id, message_id=receipt.message_id
        )
        await self._database.mark_article_delivery(article.id, DeliveryState.POSTED)
        remaining = await self._database.story_articles(original.id)
        if remaining and original.discord_thread_id:
            await self._publisher.update_story(
                original, remaining, remaining[-1], post_update=False
            )
        return story

    async def reprocess_article(self, article_id: int) -> str:
        article = await self._database.get_article(article_id)
        if article is None or article.story_id is None:
            raise ValueError("article not found or not clustered")
        if article.delivery_state is DeliveryState.UNCERTAIN:
            raise ValueError("uncertain Discord writes cannot be retried automatically")
        if article.delivery_state is not DeliveryState.FAILED:
            raise ValueError("only confirmed failed articles can be reprocessed")
        story = await self._database.get_story(article.story_id)
        if story is None:
            raise ValueError("story not found")
        if story.publication_state is PublicationState.FAILED:
            if article.feed_id is None:
                raise ValueError("article feed no longer exists")
            feed = await self._database.get_feed(article.feed_id)
            if feed is None:
                raise ValueError("article feed no longer exists")
            return await self._publish_new(feed, story, article)
        return await self._publish_update(story, article, significant=True)
