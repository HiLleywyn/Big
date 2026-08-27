from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import timedelta

from bigbot.database import Database
from bigbot.domain import Feed, FeedItem, FeedKind, utc_now
from bigbot.enrichment import EnrichmentError, OpenRouterEnricher
from bigbot.feeds.base import FeedFetchError, FeedSource
from bigbot.publisher import ForumPublisher, PublishError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollReport:
    fetched: int
    posted: int
    skipped: int
    uncertain: int


class FeedService:
    def __init__(
        self,
        *,
        database: Database,
        sources: dict[FeedKind, FeedSource],
        publisher: ForumPublisher,
        enricher: OpenRouterEnricher | None,
        tick_seconds: int,
        max_backfill: int,
    ) -> None:
        self._database = database
        self._sources = sources
        self._publisher = publisher
        self._enricher = enricher
        self._tick_seconds = tick_seconds
        self._max_backfill = max_backfill
        self._locks: dict[int, asyncio.Lock] = {}
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                due = await self._database.due_feeds(utc_now())
                for feed in due:
                    try:
                        await self.poll_feed(feed.id)
                    except Exception:
                        log.exception(
                            "feed poll crashed", extra={"event": "poll_crash", "feed_id": feed.id}
                        )
            except Exception:
                log.exception("scheduler tick failed", extra={"event": "scheduler_crash"})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._tick_seconds)

    def stop(self) -> None:
        self._stopping.set()

    async def close(self) -> None:
        self.stop()
        for source in self._sources.values():
            await source.close()
        if self._enricher:
            await self._enricher.close()

    async def poll_feed(self, feed_id: int) -> PollReport:
        lock = self._locks.setdefault(feed_id, asyncio.Lock())
        async with lock:
            feed = await self._database.get_feed(feed_id)
            if feed is None:
                raise ValueError(f"feed {feed_id} does not exist")
            source = self._sources[feed.kind]
            try:
                result = await source.fetch(feed)
            except (FeedFetchError, ValueError) as exc:
                retry_at = utc_now() + timedelta(seconds=min(feed.interval_seconds * 2, 86400))
                await self._database.record_fetch_error(
                    feed.id, next_poll_at=retry_at, error=str(exc)
                )
                log.warning(
                    "feed fetch failed",
                    extra={"event": "fetch_failed", "feed_id": feed.id},
                )
                raise

            next_poll = utc_now() + timedelta(seconds=feed.interval_seconds)
            items = result.items
            if feed.last_polled_at is None and len(items) > self._max_backfill:
                for suppressed in items[: -self._max_backfill]:
                    if await self._database.claim_delivery(feed.id, suppressed):
                        await self._database.skip_delivery(feed.id, suppressed.external_id)
                items = items[-self._max_backfill :]
            posted = skipped = uncertain = 0
            for item in items:
                if not await self._database.claim_delivery(feed.id, item):
                    skipped += 1
                    continue
                try:
                    receipt = await self._publisher.publish(feed, item)
                except PublishError as exc:
                    uncertain += 1
                    await self._database.mark_delivery_uncertain(
                        feed.id, item.external_id, error=str(exc)
                    )
                    log.error(
                        "forum publish outcome uncertain",
                        extra={
                            "event": "publish_uncertain",
                            "feed_id": feed.id,
                            "external_id": item.external_id,
                        },
                    )
                else:
                    posted += 1
                    await self._database.finish_delivery(
                        feed.id,
                        item.external_id,
                        thread_id=receipt.thread_id,
                        message_id=receipt.message_id,
                    )
                    log.info(
                        "forum item posted",
                        extra={
                            "event": "item_posted",
                            "feed_id": feed.id,
                            "external_id": item.external_id,
                        },
                    )
                    if self._enricher:
                        await self._enrich(feed, item, receipt.thread_id)

            await self._database.update_after_fetch(
                feed.id,
                next_poll_at=next_poll,
                cursor=result.cursor if result.cursor is not None else feed.cursor,
                etag=result.etag if result.etag is not None else feed.etag,
                last_modified=(
                    result.last_modified if result.last_modified is not None else feed.last_modified
                ),
                error=None if uncertain == 0 else f"{uncertain} delivery outcome(s) uncertain",
            )
            return PollReport(
                fetched=len(result.items),
                posted=posted,
                skipped=skipped,
                uncertain=uncertain,
            )

    async def _enrich(self, feed: Feed, item: FeedItem, thread_id: int) -> None:
        if self._enricher is None:
            return
        try:
            enrichment = await self._enricher.enrich(item)
            message_id = await self._publisher.reply(thread_id, enrichment.text)
        except EnrichmentError as exc:
            await self._database.record_enrichment(
                feed.id, item.external_id, state="failed", error=str(exc)
            )
            log.warning(
                "AI enrichment failed",
                extra={"event": "enrichment_failed", "feed_id": feed.id},
            )
        except PublishError as exc:
            await self._database.record_enrichment(
                feed.id, item.external_id, state="uncertain", error=str(exc)
            )
            log.error(
                "AI reply outcome uncertain",
                extra={"event": "enrichment_uncertain", "feed_id": feed.id},
            )
        else:
            await self._database.record_enrichment(
                feed.id, item.external_id, state="posted", message_id=message_id
            )
