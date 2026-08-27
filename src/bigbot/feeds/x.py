from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx2

from bigbot.domain import Feed, FeedItem, FetchResult
from bigbot.feeds.base import FeedFetchError

USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def normalize_username(value: str) -> str:
    username = value.strip().removeprefix("@")
    if not USERNAME.fullmatch(username):
        raise ValueError("X username must contain 1-15 letters, numbers, or underscores")
    return username


class XSource:
    def __init__(
        self,
        *,
        bearer_token: str | None,
        timeout_seconds: int,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._token = bearer_token
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(
            base_url="https://api.x.com",
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "BigFeedBot/0.1"},
        )
        self._users: dict[str, tuple[str, str]] = {}

    async def fetch(self, feed: Feed) -> FetchResult:
        if not self._token:
            raise FeedFetchError("X_BEARER_TOKEN is not configured")
        username = normalize_username(feed.source)
        user_id, display_name = await self._lookup_user(username)
        params: dict[str, str | int] = {
            "max_results": 10,
            "tweet.fields": "id,text,created_at,author_id",
        }
        excluded = []
        if not feed.include_replies:
            excluded.append("replies")
        if not feed.include_reposts:
            excluded.append("retweets")
        if excluded:
            params["exclude"] = ",".join(excluded)
        if feed.cursor:
            params["since_id"] = feed.cursor
        payload = await self._request(f"/2/users/{user_id}/tweets", params=params)
        items = tuple(
            sorted(
                (_post_to_item(post, username, display_name) for post in payload.get("data", [])),
                key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
            )
        )
        cursor = feed.cursor
        ids = [int(item.external_id) for item in items if item.external_id.isdigit()]
        if ids:
            cursor = str(max(ids))
        return FetchResult(items=items, cursor=cursor)

    async def _lookup_user(self, username: str) -> tuple[str, str]:
        key = username.lower()
        if key not in self._users:
            payload = await self._request(
                f"/2/users/by/username/{username}", params={"user.fields": "id,name,username"}
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not data.get("id"):
                raise FeedFetchError(f"X user @{username} was not found")
            self._users[key] = (str(data["id"]), str(data.get("name") or username))
        return self._users[key]

    async def _request(self, path: str, *, params: dict[str, str | int]) -> dict[str, Any]:
        try:
            response = await self._client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx2.HTTPError as exc:
            raise FeedFetchError(f"X request failed: {type(exc).__name__}") from exc
        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset", "unknown")
            raise FeedFetchError(f"X rate limit reached (reset {reset})")
        if response.status_code in {401, 403}:
            raise FeedFetchError("X rejected the configured credentials or access tier")
        if response.status_code == 404:
            raise FeedFetchError("X resource was not found")
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx2.HTTPError, ValueError) as exc:
            raise FeedFetchError(
                f"X API returned an invalid response ({response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise FeedFetchError("X API returned an invalid response body")
        return payload

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _post_to_item(post: dict[str, Any], username: str, display_name: str) -> FeedItem:
    post_id = str(post.get("id") or "")
    if not post_id:
        raise FeedFetchError("X post is missing its ID")
    text = str(post.get("text") or "")
    created_at: datetime | None = None
    if post.get("created_at"):
        try:
            created_at = datetime.fromisoformat(str(post["created_at"]).replace("Z", "+00:00"))
        except ValueError:
            created_at = None
    preview = " ".join(text.split())[:80] or "New post"
    return FeedItem(
        external_id=post_id,
        title=f"@{username}: {preview}",
        url=f"https://x.com/{username}/status/{post_id}",
        summary=text,
        author=display_name,
        published_at=created_at,
    )
