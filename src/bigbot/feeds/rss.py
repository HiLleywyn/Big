from __future__ import annotations

import calendar
import hashlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser  # type: ignore[import-untyped]
import httpx2

from bigbot.domain import Feed, FeedItem, FetchResult
from bigbot.feeds.base import FeedFetchError
from bigbot.security import Resolver, resolve_host, safe_external_link, validate_feed_url


class RssSource:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_bytes: int,
        resolver: Resolver = resolve_host,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._resolver = resolver
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={
                "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
                "User-Agent": "BigFeedBot/0.1 (+https://github.com/HiLleywyn/Big)",
            },
        )

    async def fetch(self, feed: Feed) -> FetchResult:
        url = await validate_feed_url(feed.source, resolver=self._resolver)
        headers: dict[str, str] = {}
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified
        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code == 304:
                    return FetchResult(
                        items=(),
                        etag=feed.etag,
                        last_modified=feed.last_modified,
                        not_modified=True,
                    )
                if 300 <= response.status_code < 400:
                    raise FeedFetchError(
                        "RSS redirects are rejected; configure the final HTTPS URL"
                    )
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > self._max_bytes:
                    raise FeedFetchError("RSS response exceeds the configured size limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise FeedFetchError("RSS response exceeds the configured size limit")
        except FeedFetchError:
            raise
        except (httpx2.HTTPError, ValueError) as exc:
            raise FeedFetchError(f"RSS request failed: {type(exc).__name__}") from exc

        items = parse_rss_bytes(bytes(body))
        return FetchResult(
            items=items,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def parse_rss_bytes(body: bytes) -> tuple[FeedItem, ...]:
    parsed = feedparser.parse(body)
    if parsed.bozo and not parsed.entries:
        raise FeedFetchError("RSS document could not be parsed")
    items = [_entry_to_item(entry, parsed.feed) for entry in parsed.entries]
    if any(item.published_at for item in items):
        items.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC))
    else:
        # Feeds conventionally return newest first. Reverse that order so forum backfills
        # read naturally and the final slice still selects the newest entries.
        items.reverse()
    return tuple(items)


def _entry_to_item(entry: Any, feed: Any) -> FeedItem:
    title = str(entry.get("title") or "New feed item")
    link = safe_external_link(str(entry.get("link") or ""))
    summary = str(entry.get("summary") or entry.get("description") or title)
    published_at = _entry_time(entry)
    identity = str(entry.get("id") or entry.get("guid") or link)
    if not identity:
        raw = f"{title}\n{entry.get('published', '')}\n{summary}".encode()
        identity = "sha256:" + hashlib.sha256(raw).hexdigest()
    author = str(entry.get("author") or feed.get("title") or "") or None
    return FeedItem(
        external_id=identity[:2048],
        title=title,
        url=link,
        summary=summary,
        author=author,
        published_at=published_at,
        image_url=_entry_image(entry),
    )


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            try:
                parsed = parsedate_to_datetime(str(value))
                return parsed.astimezone(UTC)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _entry_image(entry: Any) -> str | None:
    candidates: list[str] = []
    for value in entry.get("media_thumbnail", []):
        candidates.append(str(value.get("url") or ""))
    for value in entry.get("media_content", []):
        if str(value.get("medium") or "").lower() == "image" or str(
            value.get("type") or ""
        ).startswith("image/"):
            candidates.append(str(value.get("url") or ""))
    for value in entry.get("enclosures", []):
        if str(value.get("type") or "").startswith("image/"):
            candidates.append(str(value.get("href") or value.get("url") or ""))
    for candidate in candidates:
        safe = safe_external_link(candidate)
        if safe:
            return safe
    return None
