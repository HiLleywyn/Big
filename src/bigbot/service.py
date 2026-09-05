from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from bigbot.analysis_format import analysis_sections, repeats_reference
from bigbot.classification import TAG_CATALOG, StoryClassifier
from bigbot.clustering import StoryClusterer
from bigbot.database import Database
from bigbot.domain import (
    AnalysisState,
    Article,
    DeliveryState,
    Feed,
    FeedItem,
    FeedKind,
    PublicationState,
    Story,
    StoryState,
    WeeklyCandidate,
    utc_now,
)
from bigbot.enrichment import EnrichmentError, StoryAnalyzer
from bigbot.feeds.base import FeedFetchError, FeedSource
from bigbot.normalization import NormalizedArticle, contains_source_artifacts, normalize_item
from bigbot.publisher import ForumPublisher, PublishError
from bigbot.weekly import is_publication_worthy, select_weekly_stories

log = logging.getLogger(__name__)
PRESENTATION_VERSION = 10


def _tags_for_state(tags: tuple[str, ...], state: StoryState) -> tuple[str, ...]:
    state_tag = {
        StoryState.BREAKING: "Breaking",
        StoryState.DEVELOPING: "Developing",
    }.get(state)
    if state_tag is None:
        return tags
    return tuple(dict.fromkeys((state_tag, *tags)))[:5]


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


@dataclass(frozen=True)
class ProcessedItem:
    outcome: str
    article: Article
    story: Story
    related_stories: tuple[Story, ...]


@dataclass(frozen=True)
class ClusterMaintenanceReport:
    merged: int = 0
    split: int = 0


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
        analyzer: StoryAnalyzer | None = None,
        related_story_limit: int = 8,
        automatic_cluster_management: bool = True,
        cluster_merge_threshold: float = 0.82,
        cluster_split_threshold: float = 0.45,
        cluster_maintenance_interval_seconds: int = 1800,
        cluster_maintenance_batch_size: int = 50,
        weekly_summary_enabled: bool = True,
        weekly_summary_weekday: int = 5,
        weekly_summary_hour: int = 12,
        weekly_summary_timezone: str = "America/Chicago",
        weekly_summary_max_stories: int = 8,
        quality_gate_enabled: bool = False,
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
        self._analyzer = analyzer
        self._related_story_limit = related_story_limit
        self._automatic_cluster_management = automatic_cluster_management
        self._cluster_merge_threshold = cluster_merge_threshold
        self._cluster_split_threshold = cluster_split_threshold
        self._cluster_maintenance_interval = timedelta(seconds=cluster_maintenance_interval_seconds)
        self._cluster_maintenance_batch_size = cluster_maintenance_batch_size
        self._next_cluster_maintenance = utc_now()
        self._weekly_summary_enabled = weekly_summary_enabled
        self._weekly_summary_weekday = weekly_summary_weekday
        self._weekly_summary_hour = weekly_summary_hour
        self._weekly_summary_timezone = ZoneInfo(weekly_summary_timezone)
        self._weekly_summary_max_stories = weekly_summary_max_stories
        self._quality_gate_enabled = quality_gate_enabled
        self._next_weekly_check = utc_now()
        self._weekly_summary_first_check = True
        self._feed_locks: dict[int, asyncio.Lock] = {}
        self._story_locks: dict[int, asyncio.Lock] = {}
        self._cluster_lock = asyncio.Lock()
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._database.mark_stale_stories(utc_now() - self._stale_after)
                await self.cleanup_old_stories()
                if (
                    self._automatic_cluster_management
                    and utc_now() >= self._next_cluster_maintenance
                ):
                    await self.maintain_story_clusters()
                    self._next_cluster_maintenance = utc_now() + self._cluster_maintenance_interval
                if self._weekly_summary_enabled and utc_now() >= self._next_weekly_check:
                    await self.publish_due_weekly_summaries(force=self._weekly_summary_first_check)
                    self._weekly_summary_first_check = False
                    self._next_weekly_check = utc_now() + timedelta(minutes=5)
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

    async def publish_due_weekly_summaries(
        self, *, now: datetime | None = None, force: bool = False
    ) -> int:
        if not self._weekly_summary_enabled:
            return 0
        current = (now or utc_now()).astimezone(UTC)
        week_start, scheduled = self._weekly_period(current)
        feeds = await self._database.list_feeds()
        destinations: dict[tuple[int, int], Feed] = {}
        for feed in feeds:
            if feed.state.value == "active":
                destinations.setdefault((feed.guild_id, feed.forum_channel_id), feed)
        published = 0
        for (guild_id, forum_channel_id), feed in destinations.items():
            existing = await self._database.weekly_summary_for_period(
                guild_id=guild_id,
                forum_channel_id=forum_channel_id,
                week_start=week_start,
            )
            latest = await self._database.latest_weekly_summary(
                guild_id=guild_id,
                forum_channel_id=forum_channel_id,
            )
            bootstrap = latest is None and existing is None
            if existing is not None and existing.delivery_state is DeliveryState.UNCERTAIN:
                log.error(
                    "weekly summary has uncertain Discord state and will not be retried",
                    extra={
                        "event": "weekly_summary_uncertain",
                        "summary_id": existing.id,
                    },
                )
                continue
            retry = existing is not None and existing.delivery_state is DeliveryState.FAILED
            due = current >= scheduled and (
                existing is None or existing.generated_at < scheduled or retry
            )
            if not force and not bootstrap and not due:
                continue
            candidates = await self._database.weekly_candidate_stories(
                guild_id=guild_id,
                forum_channel_id=forum_channel_id,
                since=week_start,
                until=current + timedelta(microseconds=1),
                limit=max(60, self._weekly_summary_max_stories * 10),
            )
            selected = select_weekly_stories(
                candidates,
                limit=self._weekly_summary_max_stories,
            )
            stories = [candidate.story for candidate in selected]
            if not stories:
                continue
            local_start = week_start.astimezone(self._weekly_summary_timezone)
            local_end = current.astimezone(self._weekly_summary_timezone)
            title = (
                f"Weekly Summary | {local_start.strftime('%b')} {local_start.day} to "
                f"{local_end.strftime('%b')} {local_end.day}, {local_end.year}"
            )
            overview = (
                "The most consequential stories across Big's news sources this week. "
                "Each entry links to its complete source list, updates, and discussion."
            )
            summary = await self._database.save_weekly_summary(
                guild_id=guild_id,
                forum_channel_id=forum_channel_id,
                week_start=week_start,
                week_end=current,
                title=title,
                overview=overview,
                story_ids=tuple(story.id for story in stories),
            )
            source_counts = {candidate.story.id: candidate.source_count for candidate in selected}
            try:
                receipt = await self._publisher.publish_weekly_summary(
                    feed,
                    summary,
                    stories,
                    source_counts,
                )
            except PublishError as exc:
                await self._database.mark_weekly_summary_failed(
                    summary.id,
                    error=str(exc),
                    uncertain=exc.uncertain,
                )
                log.exception(
                    "weekly summary publish failed",
                    extra={"event": "weekly_summary_failed", "summary_id": summary.id},
                )
                continue
            await self._database.mark_weekly_summary_published(
                summary.id,
                thread_id=receipt.thread_id,
                message_id=receipt.message_id,
            )
            if latest is not None and latest.id != summary.id:
                await self._publisher.unpin_weekly_summary(latest)
            published += 1
            log.info(
                "weekly summary published",
                extra={
                    "event": "weekly_summary_published",
                    "summary_id": summary.id,
                    "story_count": len(stories),
                    "scheduled_for": scheduled.isoformat(),
                },
            )
        return published

    def _weekly_period(self, now: datetime) -> tuple[datetime, datetime]:
        local = now.astimezone(self._weekly_summary_timezone)
        period_start_weekday = (self._weekly_summary_weekday + 1) % 7
        days_since_start = (local.weekday() - period_start_weekday) % 7
        start_date = local.date() - timedelta(days=days_since_start)
        start_local = datetime.combine(start_date, time.min, self._weekly_summary_timezone)
        scheduled_date = start_date + timedelta(days=6)
        scheduled_local = datetime.combine(
            scheduled_date,
            time(hour=self._weekly_summary_hour),
            self._weekly_summary_timezone,
        )
        return start_local.astimezone(UTC), scheduled_local.astimezone(UTC)

    async def maintain_story_clusters(self) -> ClusterMaintenanceReport:
        if not self._automatic_cluster_management:
            return ClusterMaintenanceReport()
        merged = 0
        split = 0
        operation_limit = max(1, min(5, self._cluster_maintenance_batch_size // 10))
        for _ in range(operation_limit):
            split_result = await self._automatic_split_once()
            if split_result is None:
                break
            original, new_story, article = split_result
            await self._finalize_split(original, new_story, article)
            split += 1
        for _ in range(operation_limit - split):
            merge_result = await self._automatic_merge_once()
            if merge_result is None:
                break
            target, source = merge_result
            await self._finalize_merge(target, source)
            merged += 1
        log.info(
            "automatic cluster maintenance completed",
            extra={
                "event": "cluster_maintenance_completed",
                "merged": merged,
                "split": split,
            },
        )
        return ClusterMaintenanceReport(merged=merged, split=split)

    async def _automatic_split_once(self) -> tuple[Story, Story, Article] | None:
        async with self._cluster_lock:
            stories = await self._database.stories_for_cluster_maintenance(
                since=utc_now() - self._window,
                limit=self._cluster_maintenance_batch_size,
            )
            protected_since = utc_now() - timedelta(days=7)
            for story in stories:
                articles = await self._database.story_articles(story.id)
                if len(articles) < 2 or await self._database.has_recent_manual_cluster_action(
                    (story.id,), since=protected_since
                ):
                    continue
                outlier = self._clusterer.find_outlier(
                    articles,
                    threshold=self._cluster_split_threshold,
                )
                if outlier is None:
                    continue
                result = await self._database.split_article(
                    outlier.article.id,
                    actor_id=None,
                    action="automatic_split",
                    detail={
                        "score": round(outlier.score, 4),
                        "reason": outlier.reason,
                    },
                )
                log.info(
                    "story source automatically split",
                    extra={
                        "event": "story_automatic_split",
                        "story_id": story.id,
                        "article_id": outlier.article.id,
                        "cluster_score": round(outlier.score, 4),
                    },
                )
                return result
        return None

    async def _automatic_merge_once(self) -> tuple[Story, Story] | None:
        async with self._cluster_lock:
            stories = await self._database.stories_for_cluster_maintenance(
                since=utc_now() - self._window,
                limit=self._cluster_maintenance_batch_size,
            )
            protected_since = utc_now() - timedelta(days=7)
            article_sets = {
                story.id: await self._database.story_articles(story.id) for story in stories
            }
            best: tuple[float, Story, Story] | None = None
            for index, left in enumerate(stories):
                for right in stories[index + 1 :]:
                    if (left.guild_id, left.forum_channel_id) != (
                        right.guild_id,
                        right.forum_channel_id,
                    ):
                        continue
                    if await self._database.has_recent_manual_cluster_action(
                        (left.id, right.id), since=protected_since
                    ):
                        continue
                    score = self._clusterer.story_merge_score(
                        article_sets[left.id], article_sets[right.id]
                    )
                    if score < self._cluster_merge_threshold:
                        continue
                    target, source = self._canonical_merge_order(
                        left,
                        right,
                        len(article_sets[left.id]),
                        len(article_sets[right.id]),
                    )
                    if best is None or score > best[0]:
                        best = score, target, source
            if best is None:
                return None
            score, target, source = best
            merged = await self._database.merge_stories(
                target.id,
                source.id,
                actor_id=None,
                action="automatic_merge",
                detail={"score": round(score, 4)},
            )
            log.info(
                "stories automatically merged",
                extra={
                    "event": "story_automatic_merge",
                    "story_id": target.id,
                    "source_story_id": source.id,
                    "cluster_score": round(score, 4),
                },
            )
            return merged

    @staticmethod
    def _canonical_merge_order(
        left: Story,
        right: Story,
        left_count: int,
        right_count: int,
    ) -> tuple[Story, Story]:
        left_rank = (left.discord_thread_id is not None, left_count, -left.id)
        right_rank = (right.discord_thread_id is not None, right_count, -right.id)
        return (left, right) if left_rank >= right_rank else (right, left)

    def stop(self) -> None:
        self._stopping.set()

    async def close(self) -> None:
        self.stop()
        try:
            for source in self._sources.values():
                await source.close()
        finally:
            if self._analyzer is not None:
                await self._analyzer.close()

    def analysis_model(self, guild_id: int) -> str | None:
        if self._analyzer is None:
            return None
        return self._analyzer.model_for(guild_id)

    async def configure_analysis_model(self, *, guild_id: int, model: str, actor_id: int) -> str:
        if self._analyzer is None:
            raise ValueError("OpenRouter is not configured")
        validated = await self._analyzer.validate_model(model)
        await self._database.set_openrouter_model(
            guild_id=guild_id,
            model=validated,
            actor_id=actor_id,
        )
        self._analyzer.set_model(guild_id, validated)
        await self._database.audit(
            guild_id=guild_id,
            actor_id=actor_id,
            action="news.analysis_model",
            subject=validated,
        )
        log.info(
            "story analysis model updated",
            extra={
                "event": "analysis_model_updated",
                "guild_id": guild_id,
                "actor_id": actor_id,
                "model": validated,
            },
        )
        return validated

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
                tags = _tags_for_state(tags, state)
                story, article = await self._database.create_story_with_article(
                    feed=feed,
                    item=item,
                    normalized=normalized,
                    tags=tags,
                    state=state,
                    priority=priority,
                )
                significant = True
            else:
                old_story = decision.story
                if old_story.state is StoryState.BREAKING:
                    state = StoryState.BREAKING
                elif decision.significant_update:
                    state = StoryState.DEVELOPING
                else:
                    state = StoryState.UPDATED
                tags = _tags_for_state(tags, state)
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
                significant = decision.significant_update
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

        lock = self._story_locks.setdefault(story.id, asyncio.Lock())
        async with lock:
            return await self._finalize_story(
                feed=feed,
                story=story,
                article=article,
                significant=significant,
            )

    async def process_external_item(self, feed: Feed, item: FeedItem) -> ProcessedItem:
        """Run an external article through the canonical ingestion path and resolve its story."""
        outcome = await self.process_item(feed, item)
        normalized = normalize_item(item, fallback_publisher=feed.publisher or feed.name)
        article = await self._duplicate(feed, item, normalized)
        if article is None or article.story_id is None:
            raise RuntimeError("processed article could not be resolved")
        story = await self._database.get_story(article.story_id)
        if story is None:
            raise RuntimeError("processed story could not be resolved")
        related = tuple(await self._database.related_stories(story.id))
        return ProcessedItem(
            outcome=outcome,
            article=article,
            story=story,
            related_stories=related,
        )

    async def refresh_presentation(
        self, *, forum_channel_id: int | None = None, force: bool = False
    ) -> tuple[int, int]:
        stories = await self._database.stories_for_presentation(
            version=PRESENTATION_VERSION,
            forum_channel_id=forum_channel_id,
            force=force,
        )
        updated = 0
        failed = 0
        feeds: dict[int, Feed | None] = {}
        for story in stories:
            lock = self._story_locks.setdefault(story.id, asyncio.Lock())
            async with lock:
                articles = await self._database.story_articles(story.id)
                if not articles:
                    continue
                tags: list[str] = []
                for article in articles:
                    feed = None
                    if article.feed_id is not None:
                        if article.feed_id not in feeds:
                            feeds[article.feed_id] = await self._database.get_feed(article.feed_id)
                        feed = feeds[article.feed_id]
                    normalized = normalize_item(
                        FeedItem(
                            external_id=article.external_id,
                            title=article.title,
                            url=article.url,
                            summary=article.description,
                            author=None,
                            publisher=article.publisher,
                            published_at=article.published_at,
                        ),
                        fallback_publisher=article.publisher,
                    )
                    classified = self._classifier.classify(
                        normalized,
                        feed_tags=feed.default_tags if feed is not None else (),
                    )
                    tags.extend(classified)
                known_tags = tuple(tag for tag in story.tags if tag in TAG_CATALOG)
                resolved_tags = tuple(dict.fromkeys((*tags, *known_tags)))[:5]
                resolved_tags = _tags_for_state(resolved_tags, story.state)
                current = replace(story, tags=resolved_tags)
                try:
                    await self._publisher.update_story(
                        current,
                        articles,
                        articles[-1],
                        await self._database.related_stories(story.id),
                        await self._database.story_updates(story.id),
                        post_update=False,
                    )
                except PublishError:
                    failed += 1
                    log.exception(
                        "story presentation refresh failed",
                        extra={"event": "presentation_refresh_failed", "story_id": story.id},
                    )
                    continue
                await self._database.save_story_presentation(
                    story.id,
                    tags=resolved_tags,
                    version=PRESENTATION_VERSION,
                )
                updated += 1
        log.info(
            "story presentation refresh completed",
            extra={
                "event": "presentation_refresh_completed",
                "updated": updated,
                "failed": failed,
                "forum_channel_id": forum_channel_id,
            },
        )
        return updated, failed

    async def recover_story_analysis(self) -> tuple[int, int]:
        if self._analyzer is None:
            return 0, 0
        stories = await self._database.published_stories_needing_analysis()
        semaphore = asyncio.Semaphore(2)

        async def recover(story: Story) -> bool:
            async with semaphore:
                return await self._recover_story_analysis(story)

        results = await asyncio.gather(*(recover(story) for story in stories))
        ready = sum(results)
        failed = len(results) - ready
        log.info(
            "story analysis recovery completed",
            extra={
                "event": "story_analysis_recovery_completed",
                "ready": ready,
                "failed": failed,
            },
        )
        return ready, failed

    async def recover_pending_stories(self) -> tuple[int, int]:
        stories = await self._database.pending_stories_for_recovery()
        published = 0
        failed = 0
        for story in stories:
            articles = await self._database.story_articles(story.id)
            if not articles:
                failed += 1
                continue
            article = articles[-1]
            if article.feed_id is None:
                failed += 1
                continue
            feed = await self._database.get_feed(article.feed_id)
            if feed is None:
                failed += 1
                continue
            lock = self._story_locks.setdefault(story.id, asyncio.Lock())
            async with lock:
                outcome = await self._finalize_story(
                    feed=feed,
                    story=story,
                    article=article,
                    significant=False,
                    allow_update_message=False,
                )
            if outcome == "new_stories":
                published += 1
            else:
                failed += 1
        log.info(
            "pending story recovery completed",
            extra={
                "event": "pending_story_recovery_completed",
                "published": published,
                "failed": failed,
            },
        )
        return published, failed

    async def _recover_story_analysis(self, story: Story) -> bool:
        articles = await self._database.story_articles(story.id)
        if not articles:
            return False
        article = articles[-1]
        if article.feed_id is None:
            return False
        feed = await self._database.get_feed(article.feed_id)
        if feed is None:
            return False
        lock = self._story_locks.setdefault(story.id, asyncio.Lock())
        async with lock:
            await self._finalize_story(
                feed=feed,
                story=story,
                article=article,
                significant=False,
                allow_update_message=False,
            )
        current = await self._database.get_story(story.id)
        return current is not None and current.analysis_state is AnalysisState.READY

    async def _finalize_story(
        self,
        *,
        feed: Feed,
        story: Story,
        article: Article,
        significant: bool,
        allow_update_message: bool = True,
    ) -> str:
        articles = await self._database.story_articles(story.id)
        model_quality_error: str | None = None
        summarization_enabled = await self._database.story_summarization_enabled(story.id)
        if self._analyzer is not None and summarization_enabled:
            candidates = await self._database.relationship_candidates(
                story,
                since=utc_now() - self._window,
                limit=self._related_story_limit,
            )
            try:
                result = await self._analyzer.analyze_story(
                    story,
                    articles,
                    candidates,
                    focus_article_id=article.id,
                )
                await self._database.save_story_analysis(
                    story.id,
                    analysis=result.text,
                    related_story_ids=result.related_story_ids,
                )
                if result.latest_update:
                    await self._database.save_story_update_detail(
                        story.id, article.id, result.latest_update
                    )
                if not result.publication_suitable:
                    model_quality_error = result.publication_reason
                log.info(
                    "story analysis updated",
                    extra={
                        "event": "story_analysis_ready",
                        "story_id": story.id,
                        "source_count": len(articles),
                        "related_count": len(result.related_story_ids),
                    },
                )
            except (EnrichmentError, ValueError) as exc:
                await self._database.mark_story_analysis_failed(story.id, error=str(exc))
                log.warning(
                    "story analysis failed",
                    extra={
                        "event": "story_analysis_failed",
                        "story_id": story.id,
                        "source_count": len(articles),
                        "error_type": type(exc).__name__,
                    },
                )
        elif not summarization_enabled and (
            story.analysis is not None or story.analysis_state is not AnalysisState.DISABLED
        ):
            await self._database.clear_story_analysis(story.id)
            log.info(
                "story summary disabled by feed settings",
                extra={
                    "event": "story_summary_disabled",
                    "story_id": story.id,
                    "source_count": len(articles),
                },
            )
        current = await self._database.get_story(story.id)
        if current is None:
            raise RuntimeError("story disappeared during finalization")
        related = await self._database.related_stories(current.id)
        updates = await self._database.story_updates(current.id)
        creating = current.discord_thread_id is None
        if creating and summarization_enabled and self._analyzer is not None:
            quality_error = _publication_quality_error(current, articles)
            if quality_error is not None:
                await self._database.mark_story_publication(current.id, PublicationState.FAILED)
                await self._database.mark_article_delivery(
                    article.id,
                    DeliveryState.SKIPPED,
                    error=quality_error,
                )
                log.info(
                    "story withheld by publication quality gate",
                    extra={
                        "event": "story_quality_withheld",
                        "story_id": current.id,
                        "article_id": article.id,
                        "reason": quality_error,
                    },
                )
                return "skipped"
        if creating and self._quality_gate_enabled:
            quality_error = model_quality_error or _newsworthiness_error(current, articles)
            if quality_error is not None:
                await self._database.mark_story_publication(current.id, PublicationState.FAILED)
                await self._database.mark_article_delivery(
                    article.id,
                    DeliveryState.SKIPPED,
                    error=quality_error,
                )
                log.info(
                    "story withheld by newsworthiness gate",
                    extra={
                        "event": "story_newsworthiness_withheld",
                        "story_id": current.id,
                        "article_id": article.id,
                        "reason": quality_error,
                    },
                )
                return "skipped"
        try:
            if creating:
                receipt = await self._publisher.create_story(
                    feed, current, articles, related, updates
                )
                await self._database.mark_story_published(
                    current.id,
                    thread_id=receipt.thread_id,
                    message_id=receipt.message_id,
                )
                message_id = None
            else:
                post_update = allow_update_message and (
                    self._post_source_updates or (significant and self._post_major_updates)
                )
                message_id = await self._publisher.update_story(
                    current,
                    articles,
                    article,
                    related,
                    updates,
                    post_update=post_update,
                )
        except PublishError as exc:
            delivery = DeliveryState.UNCERTAIN if exc.uncertain else DeliveryState.FAILED
            if creating:
                publication = (
                    PublicationState.UNCERTAIN if exc.uncertain else PublicationState.FAILED
                )
                await self._database.mark_story_publication(current.id, publication)
            await self._database.mark_article_delivery(article.id, delivery, error=str(exc))
            return "uncertain" if exc.uncertain else "failed"
        await self._database.mark_article_delivery(
            article.id,
            DeliveryState.POSTED,
            update_message_id=message_id,
        )
        published = await self._database.get_story(current.id)
        if published is not None:
            await self._refresh_related_story_posts(published)
        if creating:
            log.info(
                "new story published",
                extra={
                    "event": "story_created",
                    "story_id": current.id,
                    "article_id": article.id,
                },
            )
            return "new_stories"
        return "updated_stories"

    async def _refresh_related_story_posts(self, story: Story) -> None:
        for related in await self._database.related_stories(story.id):
            if related.discord_thread_id is None:
                continue
            articles = await self._database.story_articles(related.id)
            if not articles:
                continue
            reciprocal = await self._database.related_stories(related.id)
            try:
                await self._publisher.update_story(
                    related,
                    articles,
                    articles[-1],
                    reciprocal,
                    await self._database.story_updates(related.id),
                    post_update=False,
                )
            except PublishError:
                log.exception(
                    "related story backlink update failed",
                    extra={
                        "event": "related_story_update_failed",
                        "story_id": related.id,
                        "related_story_id": story.id,
                    },
                )

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
        async with self._cluster_lock:
            target, source = await self._database.merge_stories(
                target_id, source_id, actor_id=actor_id
            )
        await self._finalize_merge(target, source)
        return (await self._database.get_story(target.id)) or target

    async def _finalize_merge(self, target: Story, source: Story) -> None:
        articles = await self._database.story_articles(target.id)
        if articles:
            article = articles[-1]
            if article.feed_id is None:
                raise ValueError("article feed no longer exists")
            feed = await self._database.get_feed(article.feed_id)
            if feed is None:
                raise ValueError("article feed no longer exists")
            await self._finalize_story(
                feed=feed,
                story=target,
                article=article,
                significant=True,
                allow_update_message=False,
            )
        await self._publisher.mark_merged(source, target)

    async def split_article(self, article_id: int, *, actor_id: int) -> Story:
        async with self._cluster_lock:
            original, story, article = await self._database.split_article(
                article_id, actor_id=actor_id
            )
        await self._finalize_split(original, story, article)
        return (await self._database.get_story(story.id)) or story

    async def _finalize_split(self, original: Story, story: Story, article: Article) -> None:
        if article.feed_id is None:
            raise ValueError("article feed no longer exists")
        feed = await self._database.get_feed(article.feed_id)
        if feed is None:
            raise ValueError("article feed no longer exists")
        await self._database.mark_article_delivery(article.id, DeliveryState.PENDING)
        await self._finalize_story(
            feed=feed,
            story=story,
            article=article,
            significant=True,
            allow_update_message=False,
        )
        remaining = await self._database.story_articles(original.id)
        if remaining and original.discord_thread_id:
            remaining_article = remaining[-1]
            if remaining_article.feed_id is not None:
                remaining_feed = await self._database.get_feed(remaining_article.feed_id)
                if remaining_feed is not None:
                    await self._finalize_story(
                        feed=remaining_feed,
                        story=original,
                        article=remaining_article,
                        significant=True,
                        allow_update_message=False,
                    )

    async def reprocess_article(self, article_id: int) -> str:
        article = await self._database.get_article(article_id)
        if article is None or article.story_id is None:
            raise ValueError("article not found or not clustered")
        if article.delivery_state is DeliveryState.UNCERTAIN:
            raise ValueError("uncertain Discord writes cannot be retried automatically")
        story = await self._database.get_story(article.story_id)
        if story is None:
            raise ValueError("story not found")
        if (
            article.delivery_state is not DeliveryState.FAILED
            and story.analysis_state is not AnalysisState.FAILED
        ):
            raise ValueError("only confirmed delivery or analysis failures can be reprocessed")
        if article.feed_id is None:
            raise ValueError("article feed no longer exists")
        feed = await self._database.get_feed(article.feed_id)
        if feed is None:
            raise ValueError("article feed no longer exists")
        lock = self._story_locks.setdefault(story.id, asyncio.Lock())
        async with lock:
            return await self._finalize_story(
                feed=feed,
                story=story,
                article=article,
                significant=True,
                allow_update_message=False,
            )


def _publication_quality_error(story: Story, articles: list[Article]) -> str | None:
    del articles
    if story.analysis_state is AnalysisState.READY and story.analysis:
        sections = analysis_sections(story.analysis, title=story.title)
        summary = sections.summary.strip()
        if (
            len(summary) < 60
            or repeats_reference(summary, story.title)
            or contains_source_artifacts(summary)
        ):
            return "analysis did not contain a clean factual summary beyond the headline"
        statements: list[str] = []
        candidates = [
            *re.split(r'(?<=[.!?])(?:["\u201d\u2019])?\s+|;\s+', summary),
            *sections.key_facts,
            *sections.context,
        ]
        for candidate in candidates:
            detail = candidate.strip()
            if (
                len(detail) < 30
                or contains_source_artifacts(detail)
                or any(repeats_reference(detail, existing) for existing in statements)
            ):
                continue
            statements.append(detail)
        if len(statements) >= 2:
            return None
        return "analysis did not contain at least two distinct factual statements"
    if story.analysis_state is AnalysisState.FAILED:
        return "analysis failed; unverified scraped text cannot pass the publication gate"
    return "verified analysis was not available"


def _newsworthiness_error(story: Story, articles: list[Article]) -> str | None:
    publishers = {article.publisher.strip().casefold() for article in articles if article.publisher}
    candidate = WeeklyCandidate(
        story=story,
        source_count=len(publishers),
        article_count=len(articles),
    )
    if is_publication_worthy(candidate):
        return None
    return "story did not meet the configured newsworthiness threshold"
