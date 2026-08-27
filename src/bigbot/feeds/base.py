from __future__ import annotations

from typing import Protocol

from bigbot.domain import Feed, FetchResult


class FeedFetchError(RuntimeError):
    pass


class FeedSource(Protocol):
    async def fetch(self, feed: Feed) -> FetchResult: ...

    async def close(self) -> None: ...
