from __future__ import annotations

from datetime import UTC, datetime

import httpx2
import pytest

from bigbot.domain import Feed, FeedKind, FeedState
from bigbot.feeds.base import FeedFetchError
from bigbot.feeds.x import XSource, normalize_username


def feed(*, cursor: str | None = None) -> Feed:
    now = datetime.now(UTC)
    return Feed(
        1,
        2,
        3,
        "x-wire",
        FeedKind.X,
        "DebateNews",
        300,
        (),
        False,
        False,
        FeedState.ACTIVE,
        cursor,
        None,
        None,
        now,
        now,
        None,
    )


@pytest.mark.parametrize("value", ["debate", "@Debate_2026", "a"])
def test_normalize_username(value: str) -> None:
    assert normalize_username(value).isalnum() or "_" in normalize_username(value)


@pytest.mark.parametrize("value", ["", "@", "has space", "x" * 16, "bad-name"])
def test_normalize_username_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_username(value)


async def test_x_source_resolves_user_and_reads_new_posts() -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if "/by/username/" in str(request.url):
            return httpx2.Response(200, json={"data": {"id": "42", "name": "Debate News"}})
        expected_cursor = "100" if len(requests) == 2 else "102"
        assert request.url.params["since_id"] == expected_cursor
        assert request.url.params["exclude"] == "replies,retweets"
        return httpx2.Response(
            200,
            json={
                "data": [
                    {"id": "102", "text": "Later", "created_at": "2026-08-26T12:00:00Z"},
                    {"id": "101", "text": "Earlier", "created_at": "2026-08-26T11:00:00Z"},
                ]
            },
        )

    client = httpx2.AsyncClient(
        base_url="https://api.x.com", transport=httpx2.MockTransport(handler)
    )
    source = XSource(bearer_token="test-token", timeout_seconds=10, client=client)
    result = await source.fetch(feed(cursor="100"))
    assert [item.external_id for item in result.items] == ["101", "102"]
    assert result.cursor == "102"
    assert result.items[0].url == "https://x.com/DebateNews/status/101"
    assert len(requests) == 2

    # Username lookup is cached across polls.
    await source.fetch(feed(cursor="102"))
    assert len(requests) == 3
    await client.aclose()


async def test_x_source_requires_token() -> None:
    source = XSource(bearer_token=None, timeout_seconds=10)
    with pytest.raises(FeedFetchError, match="not configured"):
        await source.fetch(feed())
    await source.close()


async def test_x_source_reports_rate_limit_without_retrying() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, headers={"x-rate-limit-reset": "123"})

    client = httpx2.AsyncClient(
        base_url="https://api.x.com", transport=httpx2.MockTransport(handler)
    )
    source = XSource(bearer_token="token", timeout_seconds=10, client=client)
    with pytest.raises(FeedFetchError, match="reset 123"):
        await source.fetch(feed())
    await client.aclose()
