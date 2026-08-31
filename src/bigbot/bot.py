from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from bigbot.classification import StoryClassifier
from bigbot.clustering import DeterministicClusterer
from bigbot.config import Settings
from bigbot.database import Database, DuplicateFeedError
from bigbot.domain import Feed, FeedKind, FeedState
from bigbot.feeds.base import FeedSource
from bigbot.feeds.rss import RssSource
from bigbot.feeds.x import XSource
from bigbot.health import HealthServer
from bigbot.publisher import DiscordForumPublisher
from bigbot.security import validate_feed_url
from bigbot.service import FeedService, PollReport

log = logging.getLogger(__name__)
FEED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,49}$")


class BigCommandTree(app_commands.CommandTree[commands.Bot]):
    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "You need Manage Server permission to administer news feeds."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "News commands can only be used inside a server."
        elif isinstance(error, app_commands.CommandInvokeError) and isinstance(
            error.original, ValueError
        ):
            message = str(error.original)
        else:
            log.exception("slash command failed", exc_info=error)
            message = "The command failed safely. Check the bot logs for details."
        if interaction.response.is_done():
            await interaction.followup.send(message[:2000], ephemeral=True)
        else:
            await interaction.response.send_message(message[:2000], ephemeral=True)


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
        self._health: HealthServer | None = None

    async def setup_hook(self) -> None:
        await self.database.connect()
        await _sync_yaml_feeds(self.database, self.settings)
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
        config = self.settings.app_config
        self.feed_service = FeedService(
            database=self.database,
            sources=sources,
            publisher=DiscordForumPublisher(self),
            clusterer=DeterministicClusterer(config.clustering.threshold),
            classifier=StoryClassifier.with_defaults(config.tag_mappings),
            tick_seconds=self.settings.poll_tick_seconds,
            max_backfill=self.settings.max_backfill,
            clustering_window_hours=config.clustering.window_hours,
            stale_after_hours=config.clustering.stale_after_hours,
            source_priorities=config.source_priorities,
            post_major_updates=config.updates.post_major_updates,
            post_source_updates=config.updates.post_source_updates,
        )

        async def health_status() -> dict[str, object]:
            counts = await self.database.story_counts()
            return {
                "status": "ok",
                "ready": self.is_ready(),
                "mode": "discord",
                "feeds": len(await self.database.list_feeds()),
                "stories": sum(counts.values()),
            }

        self._health = HealthServer(
            self.settings.health_host, self.settings.health_port, health_status
        )
        await self._health.start()
        self.tree.add_command(NewsCommands(self))
        sync_guild = self.settings.guild_id or config.guild_id
        if sync_guild:
            guild = discord.Object(id=sync_guild)
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
        if self._health:
            await self._health.close()
        await self.database.close()
        await super().close()

    async def on_ready(self) -> None:
        if self._scheduler is None:
            self._scheduler = asyncio.create_task(
                self.feed_service.run(), name="big-news-scheduler"
            )
        log.info("Big is ready", extra={"event": "ready"})
        if self.user:
            log.info("Invite Big with %s", invite_url(self.user.id), extra={"event": "invite"})


class NewsCommands(app_commands.Group):
    def __init__(self, bot: BigBot) -> None:
        super().__init__(name="news", description="Manage Big's forum news system")
        self.bot = bot

    @app_commands.command(name="status", description="Show news system status")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id(interaction)
        feeds = await self.bot.database.list_feeds(guild_id)
        counts = await self.bot.database.story_counts(guild_id)
        active = sum(feed.state is FeedState.ACTIVE for feed in feeds)
        lines = [
            f"**Big status** | {active}/{len(feeds)} feeds active",
            f"Stories: {sum(counts.values())} total, {counts['breaking']} breaking, "
            f"{counts['developing']} developing, {counts['updated']} updated",
        ]
        errors = [feed for feed in feeds if feed.last_error]
        if errors:
            lines.append(f"Feeds with errors: {len(errors)}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="feeds", description="List configured RSS and Atom feeds")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def feeds(self, interaction: discord.Interaction) -> None:
        feeds = await self.bot.database.list_feeds(_guild_id(interaction))
        if not feeds:
            await interaction.response.send_message("No feeds are configured.", ephemeral=True)
            return
        lines = []
        for feed in feeds:
            error = f" | {feed.last_error[:80]}" if feed.last_error else ""
            tags = ", ".join(feed.default_tags) or "automatic tags"
            lines.append(
                f"`{feed.id}` **{feed.name}** | {feed.state.value} | <#{feed.forum_channel_id}> "
                f"| {tags}{error}"
            )
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @app_commands.command(name="add-feed", description="Add an RSS or Atom feed")
    @app_commands.describe(
        name="Short unique name",
        url="Public HTTPS RSS or Atom URL",
        forum="Destination forum channel",
        interval_minutes="Polling interval",
        default_tags="Comma-separated forum tag names",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_feed(
        self,
        interaction: discord.Interaction,
        name: str,
        url: str,
        forum: discord.ForumChannel,
        interval_minutes: app_commands.Range[int, 5, 1440] = 15,
        default_tags: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        _validate_name(name)
        await validate_feed_url(url)
        tags = _parse_tag_names(default_tags, forum)
        try:
            feed = await self.bot.database.add_feed(
                guild_id=_guild_id(interaction),
                forum_channel_id=forum.id,
                name=name.strip(),
                kind=FeedKind.RSS,
                source=url,
                interval_seconds=interval_minutes * 60,
                tag_ids=(),
                default_tags=tags,
                include_replies=False,
                include_reposts=False,
                created_by=interaction.user.id,
            )
        except DuplicateFeedError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await self.bot.database.audit(
            guild_id=_guild_id(interaction),
            actor_id=interaction.user.id,
            action="news.add_feed",
            subject=str(feed.id),
            detail={"forum_channel_id": forum.id, "url": url},
        )
        await interaction.followup.send(
            f"Added **{feed.name}** as feed `{feed.id}`. It is ready to poll.", ephemeral=True
        )

    @app_commands.command(name="remove-feed", description="Remove a configured feed")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_feed(self, interaction: discord.Interaction, feed_id: int) -> None:
        feed = await self._feed(interaction, feed_id)
        await self.bot.database.remove_feed(feed.id)
        await self.bot.database.audit(
            guild_id=_guild_id(interaction),
            actor_id=interaction.user.id,
            action="news.remove_feed",
            subject=str(feed.id),
        )
        await interaction.response.send_message(f"Removed feed `{feed.id}`.", ephemeral=True)

    @app_commands.command(name="refresh", description="Poll one feed or every feed now")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def refresh(self, interaction: discord.Interaction, feed_id: int | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        if feed_id is not None:
            feeds = [await self._feed(interaction, feed_id)]
        else:
            feeds = await self.bot.database.list_feeds(_guild_id(interaction))
        reports: list[PollReport] = []
        for feed in feeds:
            reports.append(await self.bot.feed_service.poll_feed(feed.id))
        total = PollReport(
            fetched=sum(report.fetched for report in reports),
            new_stories=sum(report.new_stories for report in reports),
            updated_stories=sum(report.updated_stories for report in reports),
            duplicates=sum(report.duplicates for report in reports),
            skipped=sum(report.skipped for report in reports),
            failed=sum(report.failed for report in reports),
            uncertain=sum(report.uncertain for report in reports),
        )
        await interaction.followup.send(_report_text(total), ephemeral=True)

    @app_commands.command(name="story", description="Inspect a story and its sources")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def story(self, interaction: discord.Interaction, story_id: int) -> None:
        story = await self.bot.database.get_story(story_id)
        if story is None or story.guild_id != _guild_id(interaction):
            raise ValueError("story not found")
        articles = await self.bot.database.story_articles(story.id)
        lines = [
            f"**Story `{story.id}`** | {story.state.value} | {story.publication_state.value}",
            story.title,
            f"Sources: {len(articles)}",
        ]
        lines.extend(
            f"`{article.id}` {article.publisher}: {article.url}" for article in articles[:12]
        )
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @app_commands.command(name="merge", description="Merge one story cluster into another")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def merge(
        self, interaction: discord.Interaction, target_story_id: int, source_story_id: int
    ) -> None:
        target = await self.bot.database.get_story(target_story_id)
        if target is None or target.guild_id != _guild_id(interaction):
            raise ValueError("target story not found")
        await interaction.response.defer(ephemeral=True)
        merged = await self.bot.feed_service.merge_stories(
            target_story_id, source_story_id, actor_id=interaction.user.id
        )
        await interaction.followup.send(
            f"Merged story `{source_story_id}` into `{merged.id}`.", ephemeral=True
        )

    @app_commands.command(name="split", description="Move an article into a new story")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def split(self, interaction: discord.Interaction, article_id: int) -> None:
        article = await self.bot.database.get_article(article_id)
        if article is None or article.story_id is None:
            raise ValueError("article not found")
        story = await self.bot.database.get_story(article.story_id)
        if story is None or story.guild_id != _guild_id(interaction):
            raise ValueError("article not found")
        await interaction.response.defer(ephemeral=True)
        new_story = await self.bot.feed_service.split_article(
            article_id, actor_id=interaction.user.id
        )
        await interaction.followup.send(
            f"Moved article `{article_id}` into new story `{new_story.id}`.", ephemeral=True
        )

    @app_commands.command(name="reprocess", description="Retry a confirmed failed article")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def reprocess(self, interaction: discord.Interaction, article_id: int) -> None:
        article = await self.bot.database.get_article(article_id)
        if article is None or article.story_id is None:
            raise ValueError("article not found")
        story = await self.bot.database.get_story(article.story_id)
        if story is None or story.guild_id != _guild_id(interaction):
            raise ValueError("article not found")
        await interaction.response.defer(ephemeral=True)
        result = await self.bot.feed_service.reprocess_article(article_id)
        await interaction.followup.send(f"Reprocess result: {result}.", ephemeral=True)

    async def _feed(self, interaction: discord.Interaction, feed_id: int) -> Feed:
        feed = await self.bot.database.get_feed(feed_id)
        if feed is None or feed.guild_id != _guild_id(interaction):
            raise ValueError("feed not found")
        return feed


async def _sync_yaml_feeds(database: Database, settings: Settings) -> None:
    config = settings.app_config
    if not config.feeds:
        return
    guild_id = config.guild_id or settings.guild_id
    if guild_id is None:
        raise ValueError("YAML feeds require guild_id")
    for spec in config.feeds:
        forum_channel_id = spec.forum_channel_id or config.forum_channel_id
        if forum_channel_id is None:
            raise ValueError(f"YAML feed {spec.name!r} requires forum_channel_id")
        await validate_feed_url(spec.url)
        await database.upsert_config_feed(
            guild_id=guild_id,
            forum_channel_id=forum_channel_id,
            name=spec.name,
            source=spec.url,
            publisher=spec.publisher,
            interval_seconds=spec.interval_seconds,
            default_tags=spec.default_tags,
        )


def _guild_id(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise app_commands.NoPrivateMessage()
    return interaction.guild_id


def _validate_name(value: str) -> None:
    if not FEED_NAME.fullmatch(value.strip()):
        raise ValueError("feed name must be 1 to 50 simple characters")


def _parse_tag_names(value: str, forum: discord.ForumChannel) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    available = {tag.name.casefold(): tag.name for tag in forum.available_tags}
    missing = [name for name in requested if name.casefold() not in available]
    if missing:
        raise ValueError(f"forum tags do not exist: {', '.join(missing)}")
    tags = tuple(available[name.casefold()] for name in requested)
    if forum.flags.require_tag and not tags:
        raise ValueError("this forum requires at least one tag")
    if len(tags) > 5:
        raise ValueError("Discord allows at most five forum tags")
    return tags


def _report_text(report: PollReport) -> str:
    return (
        f"Fetched {report.fetched}. New stories {report.new_stories}. "
        f"Updated {report.updated_stories}. Duplicates {report.duplicates}. "
        f"Skipped {report.skipped}. Failed {report.failed}. Uncertain {report.uncertain}."
    )


def invite_url(client_id: int) -> str:
    permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        create_public_threads=True,
        send_messages_in_threads=True,
        embed_links=True,
        read_message_history=True,
        manage_threads=True,
    )
    return discord.utils.oauth_url(
        client_id,
        permissions=permissions,
        scopes=("bot", "applications.commands"),
    )
