from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal

from bigbot.bot import BigBot, _sync_yaml_feeds
from bigbot.classification import StoryClassifier
from bigbot.clustering import DeterministicClusterer
from bigbot.config import ConfigurationError, Settings, load_settings
from bigbot.database import Database
from bigbot.domain import FeedKind
from bigbot.enrichment import build_story_analyzer
from bigbot.feeds.base import FeedSource
from bigbot.feeds.rss import RssSource
from bigbot.feeds.x import XSource
from bigbot.health import HealthServer
from bigbot.logging_config import configure_logging
from bigbot.public_api import StoryFeedQuery, build_story_detail, build_story_feed
from bigbot.publisher import DryRunForumPublisher
from bigbot.service import FeedService


async def _doctor() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_path)
    await database.connect()
    feeds = await database.list_feeds()
    stories = await database.story_counts()
    await database.close()
    print(
        json.dumps(
            {
                "status": "ok",
                "database": str(settings.database_path),
                "config": str(settings.config_path),
                "configured_feeds": len(settings.app_config.feeds),
                "database_feeds": len(feeds),
                "stories": sum(stories.values()),
                "discord_token": "configured" if settings.discord_token else "missing",
                "dry_run": settings.dry_run,
                "health_port": settings.health_port,
            },
            indent=2,
        )
    )


async def _dry_run(settings: Settings) -> None:
    database = Database(settings.database_path)
    await database.connect()
    await _sync_yaml_feeds(database, settings)
    config = settings.app_config
    sources: dict[FeedKind, FeedSource] = {
        FeedKind.RSS: RssSource(
            timeout_seconds=settings.http_timeout_seconds,
            max_bytes=settings.rss_max_bytes,
        ),
        FeedKind.X: XSource(
            bearer_token=settings.x_bearer_token,
            timeout_seconds=settings.http_timeout_seconds,
        ),
    }
    service = FeedService(
        database=database,
        sources=sources,
        publisher=DryRunForumPublisher(),
        clusterer=DeterministicClusterer(config.clustering.threshold),
        classifier=StoryClassifier.with_defaults(config.tag_mappings),
        tick_seconds=settings.poll_tick_seconds,
        max_backfill=settings.max_backfill,
        clustering_window_hours=config.clustering.window_hours,
        stale_after_hours=config.clustering.stale_after_hours,
        source_priorities=config.source_priorities,
        post_major_updates=config.updates.post_major_updates,
        post_source_updates=config.updates.post_source_updates,
        retention_after_days=config.retention.clear_after_days,
        retention_action=config.retention.action,
        retention_batch_size=config.retention.batch_size,
        analyzer=build_story_analyzer(settings),
        related_story_limit=settings.related_story_limit,
        automatic_cluster_management=config.clustering.automatic_management,
        cluster_merge_threshold=config.clustering.merge_threshold,
        cluster_split_threshold=config.clustering.split_threshold,
        cluster_maintenance_interval_seconds=(config.clustering.maintenance_interval_seconds),
        cluster_maintenance_batch_size=config.clustering.maintenance_batch_size,
        weekly_summary_enabled=config.weekly_summary.enabled,
        weekly_summary_weekday=config.weekly_summary.weekday,
        weekly_summary_hour=config.weekly_summary.hour,
        weekly_summary_timezone=config.weekly_summary.timezone,
        weekly_summary_max_stories=config.weekly_summary.max_stories,
    )

    async def status() -> dict[str, object]:
        feeds = await database.list_feeds()
        stories = await database.story_counts()
        return {
            "status": "ok",
            "ready": True,
            "mode": "dry-run",
            "feeds": len(feeds),
            "stories": sum(stories.values()),
        }

    async def public_story_feed(query: StoryFeedQuery) -> dict[str, object]:
        return await build_story_feed(
            database,
            query=query,
            public_site_url=settings.public_site_url,
        )

    async def public_story_detail(story_id: int) -> dict[str, object] | None:
        return await build_story_detail(
            database,
            story_id=story_id,
            public_site_url=settings.public_site_url,
        )

    health = HealthServer(
        settings.health_host,
        settings.health_port,
        status,
        story_feed_provider=public_story_feed,
        story_detail_provider=public_story_detail,
        cors_origins=settings.public_cors_origins,
    )
    await health.start()
    scheduler = asyncio.create_task(service.run(), name="big-dry-run-scheduler")
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(event, stopping.set)
    try:
        await stopping.wait()
    finally:
        service.stop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(scheduler, timeout=10)
        if not scheduler.done():
            scheduler.cancel()
        await service.close()
        await health.close()
        await database.close()


def _run() -> None:
    settings = load_settings(require_discord=True)
    configure_logging(settings.log_level)
    if settings.dry_run:
        asyncio.run(_dry_run(settings))
        return
    bot = BigBot(settings)
    bot.run(settings.discord_token or "", log_handler=None)


def main() -> None:
    parser = argparse.ArgumentParser(prog="big", description="Discord forum news system")
    parser.add_argument("command", nargs="?", choices=("run", "doctor"), default="run")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            asyncio.run(_doctor())
        else:
            _run()
    except ConfigurationError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
