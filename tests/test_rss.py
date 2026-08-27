from __future__ import annotations

import ipaddress
from datetime import UTC, datetime

import httpx2
import pytest

from bigbot.domain import Feed, FeedKind, FeedState
from bigbot.feeds.base import FeedFetchError
from bigbot.feeds.rss import RssSource, parse_rss_bytes

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Debate Wire</title>
<item><guid>a-1</guid><title>First topic</title><link>https://example.com/1</link>
<description><![CDATA[<p>The <b>first</b> summary.</p>]]></description>
<pubDate>Tue, 25 Aug 2026 10:00:00 GMT</pubDate></item>
<item><guid>a-2</guid><title>Second topic</title><link>javascript:alert(1)</link>
<description>Second summary.</description>
<pubDate>Tue, 25 Aug 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_parse_rss_preserves_order_and_identity() -> None:
    items = parse_rss_bytes(RSS)
    assert [item.external_id for item in items] == ["a-1", "a-2"]
    assert items[0].author == "Debate Wire"
    assert items[0].published_at == datetime(2026, 8, 25, 10, tzinfo=UTC)
    assert items[1].url == ""


def test_invalid_rss_fails_closed() -> None:
    with pytest.raises(FeedFetchError):
        parse_rss_bytes(b"this is not XML")


def test_undated_newest_first_feed_becomes_oldest_first() -> None:
    body = b"""<rss version="2.0"><channel><title>Wire</title>
    <item><guid>new</guid><title>New</title></item>
    <item><guid>old</guid><title>Old</title></item>
    </channel></rss>"""
    assert [item.external_id for item in parse_rss_bytes(body)] == ["old", "new"]


def feed(*, etag: str | None = None) -> Feed:
    now = datetime.now(UTC)
    return Feed(
        1,
        2,
        3,
        "rss-wire",
        FeedKind.RSS,
        "https://example.com/rss",
        300,
        (),
        False,
        False,
        FeedState.ACTIVE,
        None,
        etag,
        None,
        now,
        now,
        None,
    )


async def public_resolver(host: str, port: int) -> set[ipaddress.IPv4Address]:
    return {ipaddress.ip_address("93.184.216.34")}


async def test_rss_source_uses_conditional_get_and_parses() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["if-none-match"] == '"old"'
        return httpx2.Response(
            200,
            content=RSS,
            headers={"etag": '"new"', "last-modified": "Wed, 26 Aug 2026 12:00:00 GMT"},
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    source = RssSource(
        timeout_seconds=10,
        max_bytes=100_000,
        resolver=public_resolver,
        client=client,
    )
    result = await source.fetch(feed(etag='"old"'))
    assert len(result.items) == 2
    assert result.etag == '"new"'
    await client.aclose()


async def test_rss_source_handles_not_modified() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(304)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    source = RssSource(
        timeout_seconds=10,
        max_bytes=100_000,
        resolver=public_resolver,
        client=client,
    )
    result = await source.fetch(feed(etag='"same"'))
    assert result.not_modified
    assert result.etag == '"same"'
    await client.aclose()


async def test_rss_source_rejects_redirect_and_oversized_body() -> None:
    responses = iter(
        [
            httpx2.Response(302, headers={"location": "https://elsewhere.example/rss"}),
            httpx2.Response(200, content=b"x" * 101, headers={"content-length": "101"}),
        ]
    )

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return next(responses)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    source = RssSource(
        timeout_seconds=10,
        max_bytes=100,
        resolver=public_resolver,
        client=client,
    )
    with pytest.raises(FeedFetchError, match="redirects"):
        await source.fetch(feed())
    with pytest.raises(FeedFetchError, match="size limit"):
        await source.fetch(feed())
    await client.aclose()
