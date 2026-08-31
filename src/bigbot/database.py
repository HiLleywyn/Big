from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from bigbot.domain import (
    Article,
    DeliveryState,
    Feed,
    FeedItem,
    FeedKind,
    FeedState,
    PublicationState,
    Story,
    StoryState,
    parse_time,
    utc_now,
)
from bigbot.normalization import NormalizedArticle

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

STORY_SCHEMA = """
ALTER TABLE feeds ADD COLUMN default_tags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE feeds ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    forum_channel_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('new','developing','breaking','updated','stale','merged')),
    publication_state TEXT NOT NULL
        CHECK (publication_state IN ('pending','published','failed','uncertain')),
    discord_thread_id INTEGER UNIQUE,
    discord_starter_message_id INTEGER,
    tags_json TEXT NOT NULL DEFAULT '[]',
    normalized_title TEXT NOT NULL,
    entities_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    numbers_json TEXT NOT NULL DEFAULT '[]',
    event_terms_json TEXT NOT NULL DEFAULT '[]',
    primary_priority INTEGER NOT NULL DEFAULT 0,
    primary_article_id INTEGER,
    first_published_at TEXT,
    last_published_at TEXT,
    last_updated_at TEXT NOT NULL,
    merged_into_story_id INTEGER REFERENCES stories(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_stories_candidates
    ON stories (guild_id, forum_channel_id, last_published_at, state);

CREATE TABLE articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER REFERENCES feeds(id) ON DELETE SET NULL,
    story_id INTEGER REFERENCES stories(id) ON DELETE SET NULL,
    external_id TEXT NOT NULL,
    publisher TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    published_at TEXT,
    description TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    entities_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    numbers_json TEXT NOT NULL DEFAULT '[]',
    event_terms_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL,
    delivery_state TEXT NOT NULL
        CHECK (delivery_state IN ('pending','posted','skipped','failed','uncertain')),
    delivery_error TEXT,
    update_message_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (feed_id, external_id)
);

CREATE UNIQUE INDEX idx_articles_canonical_url
    ON articles (canonical_url) WHERE canonical_url != '';
CREATE UNIQUE INDEX idx_articles_publisher_fingerprint
    ON articles (publisher, fingerprint);
CREATE INDEX idx_articles_story ON articles (story_id, published_at, id);

CREATE TABLE story_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    article_id INTEGER REFERENCES articles(id) ON DELETE SET NULL,
    actor_id INTEGER,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

FEED_PUBLISHER_SCHEMA = """
ALTER TABLE feeds ADD COLUMN publisher TEXT NOT NULL DEFAULT '';
"""

MIGRATIONS = ((1, SCHEMA), (2, STORY_SCHEMA), (3, FEED_PUBLISHER_SCHEMA))


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
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        cursor = await connection.execute("SELECT version FROM schema_migrations")
        applied = {int(row[0]) async for row in cursor}
        for version, script in MIGRATIONS:
            if version in applied:
                continue
            await connection.executescript(script)
            await connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, utc_now().isoformat()),
            )
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
        default_tags: tuple[str, ...] = (),
        publisher: str = "",
    ) -> Feed:
        now = utc_now().isoformat()
        try:
            cursor = await self._db().execute(
                """
                INSERT INTO feeds (
                    guild_id, forum_channel_id, name, kind, source, publisher,
                    interval_seconds, tag_ids_json, include_replies, include_reposts, next_poll_at,
                    created_by, created_at, updated_at, default_tags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    forum_channel_id,
                    name,
                    kind.value,
                    source,
                    publisher,
                    interval_seconds,
                    json.dumps(tag_ids),
                    int(include_replies),
                    int(include_reposts),
                    now,
                    created_by,
                    now,
                    now,
                    json.dumps(default_tags),
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
                last_polled_at = ?, last_error = ?, updated_at = ?, failure_count = 0
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
            UPDATE feeds SET next_poll_at = ?, last_error = ?, updated_at = ?,
                failure_count = failure_count + 1
            WHERE id = ?
            """,
            (next_poll_at.isoformat(), error[:1000], now, feed_id),
        )
        await self._db().commit()

    async def upsert_config_feed(
        self,
        *,
        guild_id: int,
        forum_channel_id: int,
        name: str,
        source: str,
        publisher: str,
        interval_seconds: int,
        default_tags: tuple[str, ...],
    ) -> Feed:
        now = utc_now().isoformat()
        await self._db().execute(
            """
            INSERT INTO feeds (
                guild_id, forum_channel_id, name, kind, source, interval_seconds,
                publisher, tag_ids_json, default_tags_json, include_replies, include_reposts,
                next_poll_at, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'rss', ?, ?, ?, '[]', ?, 0, 0, ?, 0, ?, ?)
            ON CONFLICT(guild_id, name) DO UPDATE SET
                forum_channel_id = excluded.forum_channel_id,
                source = excluded.source,
                publisher = excluded.publisher,
                interval_seconds = excluded.interval_seconds,
                default_tags_json = excluded.default_tags_json,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                forum_channel_id,
                name,
                source,
                interval_seconds,
                publisher,
                json.dumps(default_tags),
                now,
                now,
                now,
            ),
        )
        await self._db().commit()
        cursor = await self._db().execute(
            "SELECT * FROM feeds WHERE guild_id = ? AND name = ?", (guild_id, name)
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("configured feed could not be read back")
        return _feed_from_row(row)

    async def find_duplicate_article(
        self,
        *,
        feed_id: int,
        external_id: str,
        canonical_url: str,
        publisher: str,
        fingerprint: str,
    ) -> Article | None:
        cursor = await self._db().execute(
            """
            SELECT * FROM articles
            WHERE (feed_id = ? AND external_id = ?)
               OR (? != '' AND canonical_url = ?)
               OR (publisher = ? AND fingerprint = ?)
            ORDER BY id LIMIT 1
            """,
            (feed_id, external_id, canonical_url, canonical_url, publisher, fingerprint),
        )
        row = await cursor.fetchone()
        return _article_from_row(row) if row is not None else None

    async def candidate_stories(
        self, *, guild_id: int, forum_channel_id: int, since: datetime
    ) -> list[Story]:
        cursor = await self._db().execute(
            """
            SELECT * FROM stories
            WHERE guild_id = ? AND forum_channel_id = ?
              AND state != 'merged' AND last_updated_at >= ?
            ORDER BY last_updated_at DESC
            """,
            (guild_id, forum_channel_id, since.isoformat()),
        )
        return [_story_from_row(row) async for row in cursor]

    async def create_story_with_article(
        self,
        *,
        feed: Feed,
        item: FeedItem,
        normalized: NormalizedArticle,
        tags: tuple[str, ...],
        state: StoryState,
        priority: int,
    ) -> tuple[Story, Article]:
        now = utc_now()
        published = item.published_at or now
        database = self._db()
        await database.execute("BEGIN IMMEDIATE")
        try:
            story_cursor = await database.execute(
                """
                INSERT INTO stories (
                    guild_id, forum_channel_id, title, summary, state, publication_state,
                    tags_json, normalized_title, entities_json, keywords_json, numbers_json,
                    event_terms_json, primary_priority, first_published_at,
                    last_published_at, last_updated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feed.guild_id,
                    feed.forum_channel_id,
                    normalized.title,
                    normalized.summary,
                    state.value,
                    json.dumps(tags),
                    normalized.normalized_title,
                    json.dumps(normalized.entities),
                    json.dumps(normalized.keywords),
                    json.dumps(normalized.numbers),
                    json.dumps(normalized.event_terms),
                    priority,
                    published.isoformat(),
                    published.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            story_id = int(story_cursor.lastrowid or 0)
            article_id = await self._insert_article(
                feed=feed,
                item=item,
                normalized=normalized,
                story_id=story_id,
                delivery_state=DeliveryState.PENDING,
            )
            await database.execute(
                "UPDATE stories SET primary_article_id = ? WHERE id = ?",
                (article_id, story_id),
            )
            await self._insert_history(story_id, "created", article_id=article_id)
            await database.commit()
        except Exception:
            await database.rollback()
            raise
        story = await self.get_story(story_id)
        article = await self.get_article(article_id)
        if story is None or article is None:
            raise RuntimeError("new story could not be read back")
        return story, article

    async def attach_article(
        self,
        *,
        story: Story,
        feed: Feed,
        item: FeedItem,
        normalized: NormalizedArticle,
        tags: tuple[str, ...],
        state: StoryState,
        priority: int,
        significant: bool,
    ) -> tuple[Story, Article]:
        now = utc_now()
        published = item.published_at or now
        database = self._db()
        await database.execute("BEGIN IMMEDIATE")
        try:
            article_id = await self._insert_article(
                feed=feed,
                item=item,
                normalized=normalized,
                story_id=story.id,
                delivery_state=DeliveryState.PENDING,
            )
            entities = tuple(sorted(set(story.entities) | set(normalized.entities)))
            keywords = tuple(sorted(set(story.keywords) | set(normalized.keywords)))[:24]
            numbers = tuple(sorted(set(story.numbers) | set(normalized.numbers)))
            events = tuple(sorted(set(story.event_terms) | set(normalized.event_terms)))
            all_tags = tuple(dict.fromkeys((*story.tags, *tags)))[:5]
            replace_primary = priority > await self._story_priority(story.id)
            await database.execute(
                """
                UPDATE stories SET
                    title = CASE WHEN ? THEN ? ELSE title END,
                    summary = CASE WHEN ? THEN ? ELSE summary END,
                    normalized_title = CASE WHEN ? THEN ? ELSE normalized_title END,
                    primary_priority = CASE WHEN ? THEN ? ELSE primary_priority END,
                    primary_article_id = CASE WHEN ? THEN ? ELSE primary_article_id END,
                    state = ?, tags_json = ?, entities_json = ?, keywords_json = ?,
                    numbers_json = ?, event_terms_json = ?, last_published_at = ?,
                    last_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    replace_primary,
                    normalized.title,
                    replace_primary,
                    normalized.summary,
                    replace_primary,
                    normalized.normalized_title,
                    replace_primary,
                    priority,
                    replace_primary,
                    article_id,
                    state.value,
                    json.dumps(all_tags),
                    json.dumps(entities),
                    json.dumps(keywords),
                    json.dumps(numbers),
                    json.dumps(events),
                    max(published, story.last_published_at or published).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    story.id,
                ),
            )
            await self._insert_history(
                story.id,
                "major_update" if significant else "source_added",
                article_id=article_id,
            )
            await database.commit()
        except Exception:
            await database.rollback()
            raise
        updated = await self.get_story(story.id)
        article = await self.get_article(article_id)
        if updated is None or article is None:
            raise RuntimeError("updated story could not be read back")
        return updated, article

    async def record_skipped_article(
        self, *, feed: Feed, item: FeedItem, normalized: NormalizedArticle
    ) -> Article:
        article_id = await self._insert_article(
            feed=feed,
            item=item,
            normalized=normalized,
            story_id=None,
            delivery_state=DeliveryState.SKIPPED,
        )
        await self._db().commit()
        article = await self.get_article(article_id)
        if article is None:
            raise RuntimeError("skipped article could not be read back")
        return article

    async def _insert_article(
        self,
        *,
        feed: Feed,
        item: FeedItem,
        normalized: NormalizedArticle,
        story_id: int | None,
        delivery_state: DeliveryState,
    ) -> int:
        now = utc_now().isoformat()
        cursor = await self._db().execute(
            """
            INSERT INTO articles (
                feed_id, story_id, external_id, publisher, title, url, canonical_url,
                published_at, description, discovered_at, normalized_title, entities_json,
                keywords_json, numbers_json, event_terms_json, fingerprint, delivery_state,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed.id,
                story_id,
                item.external_id,
                normalized.publisher,
                normalized.title,
                item.url,
                normalized.canonical_url,
                item.published_at.isoformat() if item.published_at else None,
                normalized.summary,
                now,
                normalized.normalized_title,
                json.dumps(normalized.entities),
                json.dumps(normalized.keywords),
                json.dumps(normalized.numbers),
                json.dumps(normalized.event_terms),
                normalized.fingerprint,
                delivery_state.value,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid or 0)

    async def get_story(self, story_id: int) -> Story | None:
        cursor = await self._db().execute("SELECT * FROM stories WHERE id = ?", (story_id,))
        row = await cursor.fetchone()
        return _story_from_row(row) if row is not None else None

    async def get_article(self, article_id: int) -> Article | None:
        cursor = await self._db().execute("SELECT * FROM articles WHERE id = ?", (article_id,))
        row = await cursor.fetchone()
        return _article_from_row(row) if row is not None else None

    async def story_articles(self, story_id: int) -> list[Article]:
        cursor = await self._db().execute(
            "SELECT * FROM articles WHERE story_id = ? ORDER BY published_at, id", (story_id,)
        )
        return [_article_from_row(row) async for row in cursor]

    async def mark_story_published(self, story_id: int, *, thread_id: int, message_id: int) -> None:
        await self._db().execute(
            """
            UPDATE stories SET publication_state = 'published', discord_thread_id = ?,
                discord_starter_message_id = ?, updated_at = ? WHERE id = ?
            """,
            (thread_id, message_id, utc_now().isoformat(), story_id),
        )
        await self._db().commit()

    async def mark_story_publication(self, story_id: int, state: PublicationState) -> None:
        await self._db().execute(
            "UPDATE stories SET publication_state = ?, updated_at = ? WHERE id = ?",
            (state.value, utc_now().isoformat(), story_id),
        )
        await self._db().commit()

    async def mark_article_delivery(
        self,
        article_id: int,
        state: DeliveryState,
        *,
        error: str | None = None,
        update_message_id: int | None = None,
    ) -> None:
        await self._db().execute(
            """
            UPDATE articles SET delivery_state = ?, delivery_error = ?,
                update_message_id = ?, updated_at = ? WHERE id = ?
            """,
            (
                state.value,
                error[:1000] if error else None,
                update_message_id,
                utc_now().isoformat(),
                article_id,
            ),
        )
        await self._db().commit()

    async def mark_stale_stories(self, before: datetime) -> int:
        cursor = await self._db().execute(
            """
            UPDATE stories SET state = 'stale', updated_at = ?
            WHERE state IN ('new','developing','breaking','updated') AND last_updated_at < ?
            """,
            (utc_now().isoformat(), before.isoformat()),
        )
        await self._db().commit()
        return cursor.rowcount

    async def story_counts(self, guild_id: int | None = None) -> dict[str, int]:
        if guild_id is None:
            cursor = await self._db().execute(
                "SELECT state, COUNT(*) count FROM stories GROUP BY state"
            )
        else:
            cursor = await self._db().execute(
                "SELECT state, COUNT(*) count FROM stories WHERE guild_id = ? GROUP BY state",
                (guild_id,),
            )
        result = {state.value: 0 for state in StoryState}
        async for row in cursor:
            result[str(row["state"])] = int(row["count"])
        return result

    async def merge_stories(
        self, target_id: int, source_id: int, *, actor_id: int
    ) -> tuple[Story, Story]:
        if target_id == source_id:
            raise ValueError("a story cannot be merged into itself")
        target = await self.get_story(target_id)
        source = await self.get_story(source_id)
        if target is None or source is None:
            raise ValueError("story not found")
        if target.state is StoryState.MERGED or source.state is StoryState.MERGED:
            raise ValueError("merged stories cannot be merged again")
        if (target.guild_id, target.forum_channel_id) != (
            source.guild_id,
            source.forum_channel_id,
        ):
            raise ValueError("stories must belong to the same forum")
        database = self._db()
        await database.execute("BEGIN IMMEDIATE")
        try:
            await database.execute(
                "UPDATE articles SET story_id = ? WHERE story_id = ?", (target_id, source_id)
            )
            articles = await self.story_articles(target_id)
            primary_story = source if source.primary_priority > target.primary_priority else target
            entities = tuple(sorted({value for item in articles for value in item.entities}))
            keywords = tuple(sorted({value for item in articles for value in item.keywords}))[:24]
            numbers = tuple(sorted({value for item in articles for value in item.numbers}))
            events = tuple(sorted({value for item in articles for value in item.event_terms}))
            published = [item.published_at for item in articles if item.published_at]
            tags = tuple(dict.fromkeys((*target.tags, *source.tags)))[:5]
            now = utc_now().isoformat()
            await database.execute(
                """
                UPDATE stories SET
                    title = ?, summary = ?, normalized_title = ?, primary_priority = ?,
                    primary_article_id = ?, state = 'updated', tags_json = ?,
                    entities_json = ?, keywords_json = ?, numbers_json = ?,
                    event_terms_json = ?, first_published_at = ?, last_published_at = ?,
                    last_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    primary_story.title,
                    primary_story.summary,
                    primary_story.normalized_title,
                    primary_story.primary_priority,
                    primary_story.primary_article_id,
                    json.dumps(tags),
                    json.dumps(entities),
                    json.dumps(keywords),
                    json.dumps(numbers),
                    json.dumps(events),
                    min(published).isoformat() if published else None,
                    max(published).isoformat() if published else None,
                    now,
                    now,
                    target_id,
                ),
            )
            await database.execute(
                """
                UPDATE stories
                SET state = 'merged', merged_into_story_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (target_id, now, source_id),
            )
            await self._insert_history(
                target_id,
                "manual_merge",
                actor_id=actor_id,
                detail={"source_story_id": source_id},
            )
            await database.commit()
        except Exception:
            await database.rollback()
            raise
        return (await self.get_story(target_id)) or target, (
            await self.get_story(source_id)
        ) or source

    async def split_article(
        self, article_id: int, *, actor_id: int
    ) -> tuple[Story, Story, Article]:
        article = await self.get_article(article_id)
        if article is None or article.story_id is None:
            raise ValueError("article is not assigned to a story")
        original = await self.get_story(article.story_id)
        if original is None:
            raise ValueError("original story not found")
        original_articles = await self.story_articles(original.id)
        remaining = [item for item in original_articles if item.id != article.id]
        if not remaining:
            raise ValueError("a story with one source cannot be split")
        original_primary = next(
            (item for item in remaining if item.id == original.primary_article_id), remaining[0]
        )
        now = utc_now()
        database = self._db()
        await database.execute("BEGIN IMMEDIATE")
        try:
            cursor = await database.execute(
                """
                INSERT INTO stories (
                    guild_id, forum_channel_id, title, summary, state, publication_state,
                    tags_json, normalized_title, entities_json, keywords_json, numbers_json,
                    event_terms_json, primary_priority, primary_article_id, first_published_at,
                    last_published_at, last_updated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'new', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    original.guild_id,
                    original.forum_channel_id,
                    article.title,
                    article.description,
                    json.dumps(original.tags),
                    article.normalized_title,
                    json.dumps(article.entities),
                    json.dumps(article.keywords),
                    json.dumps(article.numbers),
                    json.dumps(article.event_terms),
                    original.primary_priority if article.id == original.primary_article_id else 0,
                    article.id,
                    article.published_at.isoformat() if article.published_at else None,
                    article.published_at.isoformat() if article.published_at else None,
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            new_id = int(cursor.lastrowid or 0)
            await database.execute(
                "UPDATE articles SET story_id = ? WHERE id = ?", (new_id, article.id)
            )
            entities = tuple(sorted({value for item in remaining for value in item.entities}))
            keywords = tuple(sorted({value for item in remaining for value in item.keywords}))[:24]
            numbers = tuple(sorted({value for item in remaining for value in item.numbers}))
            events = tuple(sorted({value for item in remaining for value in item.event_terms}))
            published = [item.published_at for item in remaining if item.published_at]
            await database.execute(
                """
                UPDATE stories SET title = ?, summary = ?, normalized_title = ?,
                    primary_article_id = ?, primary_priority = ?, entities_json = ?,
                    keywords_json = ?, numbers_json = ?, event_terms_json = ?,
                    first_published_at = ?, last_published_at = ?, state = 'updated',
                    last_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    original_primary.title,
                    original_primary.description,
                    original_primary.normalized_title,
                    original_primary.id,
                    (
                        original.primary_priority
                        if original_primary.id == original.primary_article_id
                        else 0
                    ),
                    json.dumps(entities),
                    json.dumps(keywords),
                    json.dumps(numbers),
                    json.dumps(events),
                    min(published).isoformat() if published else None,
                    max(published).isoformat() if published else None,
                    now.isoformat(),
                    now.isoformat(),
                    original.id,
                ),
            )
            await self._insert_history(
                original.id, "manual_split_out", article_id=article.id, actor_id=actor_id
            )
            await self._insert_history(
                new_id, "manual_split", article_id=article.id, actor_id=actor_id
            )
            await database.commit()
        except Exception:
            await database.rollback()
            raise
        new_story = await self.get_story(new_id)
        if new_story is None:
            raise RuntimeError("split story could not be read back")
        return original, new_story, (await self.get_article(article.id)) or article

    async def _story_priority(self, story_id: int) -> int:
        cursor = await self._db().execute(
            "SELECT primary_priority FROM stories WHERE id = ?", (story_id,)
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def _insert_history(
        self,
        story_id: int,
        action: str,
        *,
        article_id: int | None = None,
        actor_id: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        await self._db().execute(
            """
            INSERT INTO story_history (
                story_id, action, article_id, actor_id, detail_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                action,
                article_id,
                actor_id,
                json.dumps(detail or {}, sort_keys=True),
                utc_now().isoformat(),
            ),
        )

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
        default_tags=tuple(str(value) for value in json.loads(str(row["default_tags_json"]))),
        failure_count=int(row["failure_count"]),
        publisher=str(row["publisher"]),
    )


def _story_from_row(row: aiosqlite.Row) -> Story:
    updated = parse_time(str(row["last_updated_at"]))
    if updated is None:
        raise ValueError("story has no update timestamp")
    return Story(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        forum_channel_id=int(row["forum_channel_id"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        state=StoryState(str(row["state"])),
        publication_state=PublicationState(str(row["publication_state"])),
        discord_thread_id=int(row["discord_thread_id"]) if row["discord_thread_id"] else None,
        discord_starter_message_id=(
            int(row["discord_starter_message_id"]) if row["discord_starter_message_id"] else None
        ),
        tags=tuple(str(value) for value in json.loads(str(row["tags_json"]))),
        normalized_title=str(row["normalized_title"]),
        entities=tuple(str(value) for value in json.loads(str(row["entities_json"]))),
        keywords=tuple(str(value) for value in json.loads(str(row["keywords_json"]))),
        numbers=tuple(str(value) for value in json.loads(str(row["numbers_json"]))),
        event_terms=tuple(str(value) for value in json.loads(str(row["event_terms_json"]))),
        first_published_at=parse_time(row["first_published_at"]),
        last_published_at=parse_time(row["last_published_at"]),
        last_updated_at=updated,
        merged_into_story_id=(
            int(row["merged_into_story_id"]) if row["merged_into_story_id"] else None
        ),
        primary_article_id=(int(row["primary_article_id"]) if row["primary_article_id"] else None),
        primary_priority=int(row["primary_priority"]),
    )


def _article_from_row(row: aiosqlite.Row) -> Article:
    discovered = parse_time(str(row["discovered_at"]))
    if discovered is None:
        discovered = datetime.now(UTC)
    return Article(
        id=int(row["id"]),
        feed_id=int(row["feed_id"]) if row["feed_id"] is not None else None,
        story_id=int(row["story_id"]) if row["story_id"] is not None else None,
        external_id=str(row["external_id"]),
        publisher=str(row["publisher"]),
        title=str(row["title"]),
        url=str(row["url"]),
        canonical_url=str(row["canonical_url"]),
        published_at=parse_time(row["published_at"]),
        description=str(row["description"]),
        discovered_at=discovered,
        normalized_title=str(row["normalized_title"]),
        entities=tuple(str(value) for value in json.loads(str(row["entities_json"]))),
        keywords=tuple(str(value) for value in json.loads(str(row["keywords_json"]))),
        numbers=tuple(str(value) for value in json.loads(str(row["numbers_json"]))),
        event_terms=tuple(str(value) for value in json.loads(str(row["event_terms_json"]))),
        fingerprint=str(row["fingerprint"]),
        delivery_state=DeliveryState(str(row["delivery_state"])),
        delivery_error=str(row["delivery_error"]) if row["delivery_error"] else None,
    )
