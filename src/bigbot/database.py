from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from bigbot.domain import DeliveryState, Feed, FeedItem, FeedKind, FeedState, parse_time, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    forum_channel_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('rss', 'x')),
    source TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds >= 300),
    tag_ids_json TEXT NOT NULL DEFAULT '[]',
    include_replies INTEGER NOT NULL DEFAULT 0,
    include_reposts INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'paused')),
    cursor TEXT,
    etag TEXT,
    last_modified TEXT,
    next_poll_at TEXT NOT NULL,
    last_polled_at TEXT,
    last_error TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (guild_id, name)
);

CREATE INDEX IF NOT EXISTS idx_feeds_due ON feeds (state, next_poll_at);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'posted', 'skipped', 'uncertain')),
    thread_id INTEGER,
    message_id INTEGER,
    error TEXT,
    enrichment_state TEXT NOT NULL DEFAULT 'disabled'
        CHECK (enrichment_state IN ('disabled', 'posted', 'failed', 'uncertain')),
    enrichment_message_id INTEGER,
    enrichment_error TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (feed_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_deliveries_feed_state ON deliveries (feed_id, state);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    subject TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DuplicateFeedError(ValueError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.execute("PRAGMA busy_timeout = 5000")
        await connection.executescript(SCHEMA)
        await connection.commit()
        self._connection = connection

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("database is not connected")
        return self._connection

    async def add_feed(
        self,
        *,
        guild_id: int,
        forum_channel_id: int,
        name: str,
        kind: FeedKind,
        source: str,
        interval_seconds: int,
        tag_ids: tuple[int, ...],
        include_replies: bool,
        include_reposts: bool,
        created_by: int,
    ) -> Feed:
        now = utc_now().isoformat()
        try:
            cursor = await self._db().execute(
                """
                INSERT INTO feeds (
                    guild_id, forum_channel_id, name, kind, source, interval_seconds,
                    tag_ids_json, include_replies, include_reposts, next_poll_at,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    forum_channel_id,
                    name,
                    kind.value,
                    source,
                    interval_seconds,
                    json.dumps(tag_ids),
                    int(include_replies),
                    int(include_reposts),
                    now,
                    created_by,
                    now,
                    now,
                ),
            )
            await self._db().commit()
        except aiosqlite.IntegrityError as exc:
            raise DuplicateFeedError(f"a feed named {name!r} already exists") from exc
        feed = await self.get_feed(int(cursor.lastrowid or 0))
        if feed is None:
            raise RuntimeError("created feed could not be read back")
        return feed

    async def get_feed(self, feed_id: int) -> Feed | None:
        cursor = await self._db().execute("SELECT * FROM feeds WHERE id = ?", (feed_id,))
        row = await cursor.fetchone()
        return _feed_from_row(row) if row is not None else None

    async def list_feeds(self, guild_id: int | None = None) -> list[Feed]:
        if guild_id is None:
            cursor = await self._db().execute("SELECT * FROM feeds ORDER BY guild_id, name")
        else:
            cursor = await self._db().execute(
                "SELECT * FROM feeds WHERE guild_id = ? ORDER BY name", (guild_id,)
            )
        return [_feed_from_row(row) async for row in cursor]

    async def due_feeds(self, now: datetime) -> list[Feed]:
        cursor = await self._db().execute(
            """
            SELECT * FROM feeds
            WHERE state = 'active' AND next_poll_at <= ?
            ORDER BY next_poll_at, id
            """,
            (now.isoformat(),),
        )
        return [_feed_from_row(row) async for row in cursor]

    async def set_feed_state(self, feed_id: int, state: FeedState) -> bool:
        cursor = await self._db().execute(
            "UPDATE feeds SET state = ?, updated_at = ? WHERE id = ?",
            (state.value, utc_now().isoformat(), feed_id),
        )
        await self._db().commit()
        return cursor.rowcount > 0

    async def remove_feed(self, feed_id: int) -> bool:
        cursor = await self._db().execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        await self._db().commit()
        return cursor.rowcount > 0

    async def update_after_fetch(
        self,
        feed_id: int,
        *,
        next_poll_at: datetime,
        cursor: str | None,
        etag: str | None,
        last_modified: str | None,
        error: str | None,
    ) -> None:
        now = utc_now().isoformat()
        await self._db().execute(
            """
            UPDATE feeds SET cursor = ?, etag = ?, last_modified = ?, next_poll_at = ?,
                last_polled_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                cursor,
                etag,
                last_modified,
                next_poll_at.isoformat(),
                now,
                error,
                now,
                feed_id,
            ),
        )
        await self._db().commit()

    async def record_fetch_error(self, feed_id: int, *, next_poll_at: datetime, error: str) -> None:
        now = utc_now().isoformat()
        await self._db().execute(
            """
            UPDATE feeds SET next_poll_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (next_poll_at.isoformat(), error[:1000], now, feed_id),
        )
        await self._db().commit()

    async def claim_delivery(self, feed_id: int, item: FeedItem) -> bool:
        now = utc_now().isoformat()
        cursor = await self._db().execute(
            """
            INSERT OR IGNORE INTO deliveries (
                feed_id, external_id, title, url, state, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed_id,
                item.external_id,
                item.title,
                item.url,
                DeliveryState.PENDING.value,
                now,
                now,
            ),
        )
        await self._db().commit()
        return cursor.rowcount > 0

    async def finish_delivery(
        self, feed_id: int, external_id: str, *, thread_id: int, message_id: int
    ) -> None:
        await self._set_delivery_state(
            feed_id,
            external_id,
            DeliveryState.POSTED,
            thread_id=thread_id,
            message_id=message_id,
            error=None,
        )

    async def skip_delivery(self, feed_id: int, external_id: str) -> None:
        await self._set_delivery_state(
            feed_id,
            external_id,
            DeliveryState.SKIPPED,
            thread_id=None,
            message_id=None,
            error="suppressed by initial backfill limit",
        )

    async def mark_delivery_uncertain(self, feed_id: int, external_id: str, *, error: str) -> None:
        await self._set_delivery_state(
            feed_id,
            external_id,
            DeliveryState.UNCERTAIN,
            thread_id=None,
            message_id=None,
            error=error[:1000],
        )

    async def record_enrichment(
        self,
        feed_id: int,
        external_id: str,
        *,
        state: str,
        message_id: int | None = None,
        error: str | None = None,
    ) -> None:
        if state not in {"posted", "failed", "uncertain"}:
            raise ValueError("invalid enrichment state")
        await self._db().execute(
            """
            UPDATE deliveries
            SET enrichment_state = ?, enrichment_message_id = ?, enrichment_error = ?,
                updated_at = ?
            WHERE feed_id = ? AND external_id = ?
            """,
            (
                state,
                message_id,
                error[:1000] if error else None,
                utc_now().isoformat(),
                feed_id,
                external_id,
            ),
        )
        await self._db().commit()

    async def _set_delivery_state(
        self,
        feed_id: int,
        external_id: str,
        state: DeliveryState,
        *,
        thread_id: int | None,
        message_id: int | None,
        error: str | None,
    ) -> None:
        await self._db().execute(
            """
            UPDATE deliveries
            SET state = ?, thread_id = ?, message_id = ?, error = ?, updated_at = ?
            WHERE feed_id = ? AND external_id = ?
            """,
            (
                state.value,
                thread_id,
                message_id,
                error,
                utc_now().isoformat(),
                feed_id,
                external_id,
            ),
        )
        await self._db().commit()

    async def delivery_counts(self, feed_id: int) -> dict[str, int]:
        cursor = await self._db().execute(
            "SELECT state, COUNT(*) AS count FROM deliveries WHERE feed_id = ? GROUP BY state",
            (feed_id,),
        )
        result = {state.value: 0 for state in DeliveryState}
        async for row in cursor:
            result[str(row["state"])] = int(row["count"])
        return result

    async def audit(
        self,
        *,
        guild_id: int,
        actor_id: int,
        action: str,
        subject: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        await self._db().execute(
            """
            INSERT INTO audit_log (guild_id, actor_id, action, subject, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                actor_id,
                action,
                subject,
                json.dumps(detail or {}, separators=(",", ":"), sort_keys=True),
                utc_now().isoformat(),
            ),
        )
        await self._db().commit()


def _feed_from_row(row: aiosqlite.Row) -> Feed:
    next_poll_at = parse_time(str(row["next_poll_at"]))
    if next_poll_at is None:
        raise ValueError("feed has no next poll timestamp")
    return Feed(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        forum_channel_id=int(row["forum_channel_id"]),
        name=str(row["name"]),
        kind=FeedKind(str(row["kind"])),
        source=str(row["source"]),
        interval_seconds=int(row["interval_seconds"]),
        tag_ids=tuple(int(value) for value in json.loads(str(row["tag_ids_json"]))),
        include_replies=bool(row["include_replies"]),
        include_reposts=bool(row["include_reposts"]),
        state=FeedState(str(row["state"])),
        cursor=str(row["cursor"]) if row["cursor"] is not None else None,
        etag=str(row["etag"]) if row["etag"] is not None else None,
        last_modified=(str(row["last_modified"]) if row["last_modified"] is not None else None),
        next_poll_at=next_poll_at,
        last_polled_at=parse_time(row["last_polled_at"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
    )
