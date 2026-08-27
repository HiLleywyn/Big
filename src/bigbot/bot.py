from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from bigbot.config import Settings
from bigbot.database import Database, DuplicateFeedError
from bigbot.domain import FeedKind, FeedState
from bigbot.enrichment import OpenRouterEnricher
from bigbot.feeds.base import FeedSource
from bigbot.feeds.rss import RssSource
from bigbot.feeds.x import XSource, normalize_username
from bigbot.publisher import DiscordForumPublisher
from bigbot.security import validate_feed_url
from bigbot.service import FeedService

log = logging.getLogger(__name__)
FEED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,49}$")


class BigCommandTree(app_commands.CommandTree[commands.Bot]):
    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need Manage Server permission to administer feeds."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "Feed commands can only be used inside a server."
        else:
            log.exception("slash command failed", exc_info=error)
            message = "The command failed safely. Check the bot logs for details."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class BigBot(commands.Bot):
    database: Database
    feed_service: FeedService

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=BigCommandTree,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.database = Database(settings.database_path)
        self._scheduler: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await self.database.connect()
        sources: dict[FeedKind, FeedSource] = {
            FeedKind.RSS: RssSource(
                timeout_seconds=self.settings.http_timeout_seconds,
                max_bytes=self.settings.rss_max_bytes,
            ),
            FeedKind.X: XSource(
                bearer_token=self.settings.x_bearer_token,
                timeout_seconds=self.settings.http_timeout_seconds,
            ),
        }
        enricher = None
        if self.settings.openrouter_api_key:
            enricher = OpenRouterEnricher(
                api_key=self.settings.openrouter_api_key,
                model=self.settings.openrouter_model,
                web_search=self.settings.ai_web_search,
                zdr=self.settings.ai_zdr,
                timeout_seconds=self.settings.http_timeout_seconds,
            )
        self.feed_service = FeedService(
            database=self.database,
            sources=sources,
            publisher=DiscordForumPublisher(self),
            enricher=enricher,
            tick_seconds=self.settings.poll_tick_seconds,
            max_backfill=self.settings.max_backfill,
        )
        self.tree.add_command(FeedCommands(self))
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        if hasattr(self, "feed_service"):
            self.feed_service.stop()
        if self._scheduler:
            try:
                await asyncio.wait_for(asyncio.shield(self._scheduler), timeout=10)
            except TimeoutError:
                self._scheduler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._scheduler
        if hasattr(self, "feed_service"):
            await self.feed_service.close()
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        if self._scheduler is None:
            self._scheduler = asyncio.create_task(
                self.feed_service.run(), name="big-feed-scheduler"
            )
        log.info(
            "Big is ready",
            extra={"event": "ready", "guild_id": self.settings.guild_id or "global"},
        )
        if self.user:
            log.info("Invite Big with %s", invite_url(self.user.id), extra={"event": "invite"})


class FeedCommands(app_commands.Group):
    def __init__(self, bot: BigBot) -> None:
        super().__init__(name="feed", description="Manage forum feed publishers")
        self.bot = bot

    @app_commands.command(name="add-rss", description="Publish an RSS or Atom feed to a forum")
    @app_commands.describe(
        name="Short unique name",
        url="Public HTTPS RSS or Atom URL",
        forum="Destination forum channel",
        interval_minutes="Polling interval (minimum 5 minutes)",
        tag_ids="Optional comma-separated forum tag IDs",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_rss(
        self,
        interaction: discord.Interaction,
        name: str,
        url: str,
        forum: discord.ForumChannel,
        interval_minutes: app_commands.Range[int, 5, 1440] = 15,
        tag_ids: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = _guild_id(interaction)
        _validate_name(name)
        tags = _parse_and_validate_tags(tag_ids, forum)
        await validate_feed_url(url)
        try:
            feed = await self.bot.database.add_feed(
                guild_id=guild_id,
                forum_channel_id=forum.id,
                name=name.strip(),
                kind=FeedKind.RSS,
                source=url,
                interval_seconds=interval_minutes * 60,
                tag_ids=tags,
                include_replies=False,
                include_reposts=False,
                created_by=interaction.user.id,
            )
        except DuplicateFeedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.bot.database.audit(
            guild_id=guild_id,
            actor_id=interaction.user.id,
            action="feed.add",
            subject=str(feed.id),
            detail={"kind": "rss", "channel_id": forum.id},
        )
        await interaction.followup.send(
            f"Added RSS feed **{feed.name}** as ID `{feed.id}`. It will poll now.",
            ephemeral=True,
        )

    @app_commands.command(name="add-x", description="Publish an X account to a forum")
    @app_commands.describe(
        name="Short unique name",
        username="X username, with or without @",
        forum="Destination forum channel",
        interval_minutes="Polling interval (minimum 5 minutes)",
        include_replies="Include replies",
        include_reposts="Include reposts",
        tag_ids="Optional comma-separated forum tag IDs",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_x(
        self,
        interaction: discord.Interaction,
        name: str,
        username: str,
        forum: discord.ForumChannel,
        interval_minutes: app_commands.Range[int, 5, 1440] = 15,
        include_replies: bool = False,
        include_reposts: bool = False,
        tag_ids: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self.bot.settings.x_bearer_token:
            await interaction.followup.send(
                "X is disabled until X_BEARER_TOKEN is configured.", ephemeral=True
            )
            return
        guild_id = _guild_id(interaction)
        _validate_name(name)
        source = normalize_username(username)
        tags = _parse_and_validate_tags(tag_ids, forum)
        try:
            feed = await self.bot.database.add_feed(
                guild_id=guild_id,
                forum_channel_id=forum.id,
                name=name.strip(),
                kind=FeedKind.X,
                source=source,
                interval_seconds=interval_minutes * 60,
                tag_ids=tags,
                include_replies=include_replies,
                include_reposts=include_reposts,
                created_by=interaction.user.id,
            )
        except DuplicateFeedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.bot.database.audit(
            guild_id=guild_id,
            actor_id=interaction.user.id,
            action="feed.add",
            subject=str(feed.id),
            detail={"kind": "x", "channel_id": forum.id, "username": source},
        )
        await interaction.followup.send(
            f"Added X feed **{feed.name}** as ID `{feed.id}`. It will poll now.",
            ephemeral=True,
        )

    @app_commands.command(name="list", description="List feeds configured in this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def list_feeds(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id(interaction)
        feeds = await self.bot.database.list_feeds(guild_id)
        if not feeds:
            await interaction.response.send_message("No feeds are configured.", ephemeral=True)
            return
        lines = []
        for feed in feeds:
            counts = await self.bot.database.delivery_counts(feed.id)
            error = f" · error: {feed.last_error[:80]}" if feed.last_error else ""
            lines.append(
                f"`{feed.id}` **{feed.name}** · {feed.kind.value} · {feed.state.value} · "
                f"<#{feed.forum_channel_id}> · {counts['posted']} posted · "
                f"{counts['uncertain']} uncertain{error}"
            )
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @app_commands.command(name="pause", description="Pause a feed")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction, feed_id: int) -> None:
        await self._set_state(interaction, feed_id, FeedState.PAUSED)

    @app_commands.command(name="resume", description="Resume a feed")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction, feed_id: int) -> None:
        await self._set_state(interaction, feed_id, FeedState.ACTIVE)

    @app_commands.command(name="remove", description="Remove a feed and its delivery history")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, feed_id: int) -> None:
        guild_id = _guild_id(interaction)
        feed = await self.bot.database.get_feed(feed_id)
        if feed is None or feed.guild_id != guild_id:
            await interaction.response.send_message("Feed not found.", ephemeral=True)
            return
        await self.bot.database.remove_feed(feed_id)
        await self.bot.database.audit(
            guild_id=guild_id,
            actor_id=interaction.user.id,
            action="feed.remove",
            subject=str(feed_id),
        )
        await interaction.response.send_message(f"Removed feed `{feed_id}`.", ephemeral=True)

    @app_commands.command(name="poll", description="Poll a feed immediately")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def poll(self, interaction: discord.Interaction, feed_id: int) -> None:
        guild_id = _guild_id(interaction)
        feed = await self.bot.database.get_feed(feed_id)
        if feed is None or feed.guild_id != guild_id:
            await interaction.response.send_message("Feed not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        report = await self.bot.feed_service.poll_feed(feed_id)
        await self.bot.database.audit(
            guild_id=guild_id,
            actor_id=interaction.user.id,
            action="feed.poll",
            subject=str(feed_id),
            detail={"posted": report.posted, "uncertain": report.uncertain},
        )
        await interaction.followup.send(
            f"Fetched {report.fetched}; posted {report.posted}; skipped {report.skipped}; "
            f"uncertain {report.uncertain}.",
            ephemeral=True,
        )

    async def _set_state(
        self, interaction: discord.Interaction, feed_id: int, state: FeedState
    ) -> None:
        guild_id = _guild_id(interaction)
        feed = await self.bot.database.get_feed(feed_id)
        if feed is None or feed.guild_id != guild_id:
            await interaction.response.send_message("Feed not found.", ephemeral=True)
            return
        await self.bot.database.set_feed_state(feed_id, state)
        await self.bot.database.audit(
            guild_id=guild_id,
            actor_id=interaction.user.id,
            action=f"feed.{state.value}",
            subject=str(feed_id),
        )
        await interaction.response.send_message(
            f"Feed `{feed_id}` is now {state.value}.", ephemeral=True
        )


def _guild_id(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise app_commands.NoPrivateMessage()
    return interaction.guild_id


def _validate_name(value: str) -> None:
    if not FEED_NAME.fullmatch(value.strip()):
        raise ValueError("feed name must be 1-50 simple characters")


def _parse_and_validate_tags(value: str, forum: discord.ForumChannel) -> tuple[int, ...]:
    if not value.strip():
        tags: tuple[int, ...] = ()
    else:
        try:
            tags = tuple(dict.fromkeys(int(part.strip()) for part in value.split(",")))
        except ValueError as exc:
            raise ValueError("tag IDs must be comma-separated numbers") from exc
    available = {tag.id for tag in forum.available_tags}
    if missing := set(tags) - available:
        raise ValueError(f"forum tag IDs do not exist: {sorted(missing)}")
    if forum.flags.require_tag and not tags:
        raise ValueError("this forum requires at least one tag")
    if len(tags) > 5:
        raise ValueError("Discord allows at most five applied forum tags")
    return tags


def invite_url(client_id: int) -> str:
    permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        create_public_threads=True,
        send_messages_in_threads=True,
        embed_links=True,
        read_message_history=True,
    )
    return discord.utils.oauth_url(
        client_id,
        permissions=permissions,
        scopes=("bot", "applications.commands"),
    )
