from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from bigbot.analysis_format import analysis_display
from bigbot.article import ArticleExtractionError, ArticleExtractor, extract_article_urls
from bigbot.classification import TAG_CATALOG, StoryClassifier
from bigbot.clustering import DeterministicClusterer
from bigbot.config import Settings
from bigbot.database import Database, DuplicateFeedError
from bigbot.domain import Feed, FeedKind, FeedState, Story
from bigbot.enrichment import (
    EnrichmentError,
    FactCheckClaim,
    FactCheckResult,
    OpenRouterEnricher,
    build_story_analyzer,
)
from bigbot.feeds.base import FeedSource
from bigbot.feeds.rss import RssSource
from bigbot.feeds.x import XSource
from bigbot.health import HealthServer
from bigbot.public_api import StoryFeedQuery, build_story_detail, build_story_feed
from bigbot.publisher import DiscordForumPublisher
from bigbot.security import forum_title, plain_text, safe_external_link, validate_feed_url
from bigbot.service import FeedService, PollReport, ProcessedItem

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
            await interaction.followup.send(
                view=NoticeView("Command Failed", (message[:2000],)),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                view=NoticeView("Command Failed", (message[:2000],)),
                ephemeral=True,
            )


class BigBot(commands.Bot):
    database: Database
    feed_service: FeedService
    article_extractor: ArticleExtractor
    enricher: OpenRouterEnricher | None

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
        self._maintenance_tasks: set[asyncio.Task[object]] = set()
        self._health: HealthServer | None = None
        self._command_sync_pending = False

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
        model_overrides = await self.database.openrouter_models()
        self.enricher = build_story_analyzer(self.settings, model_overrides=model_overrides)
        self.feed_service = FeedService(
            database=self.database,
            sources=sources,
            publisher=DiscordForumPublisher(self, public_site_url=self.settings.public_site_url),
            clusterer=DeterministicClusterer(config.clustering.threshold),
            classifier=StoryClassifier.with_defaults(config.tag_mappings),
            tick_seconds=self.settings.poll_tick_seconds,
            max_backfill=self.settings.max_backfill,
            clustering_window_hours=config.clustering.window_hours,
            stale_after_hours=config.clustering.stale_after_hours,
            source_priorities=config.source_priorities,
            post_major_updates=config.updates.post_major_updates,
            post_source_updates=config.updates.post_source_updates,
            retention_after_days=config.retention.clear_after_days,
            retention_action=config.retention.action,
            retention_batch_size=config.retention.batch_size,
            analyzer=self.enricher,
            related_story_limit=self.settings.related_story_limit,
        )
        self.article_extractor = ArticleExtractor(
            timeout_seconds=self.settings.http_timeout_seconds,
            max_bytes=self.settings.article_max_bytes,
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

        async def public_story_feed(query: StoryFeedQuery) -> dict[str, object]:
            return await build_story_feed(
                self.database,
                query=query,
                public_site_url=self.settings.public_site_url,
            )

        async def public_story_detail(story_id: int) -> dict[str, object] | None:
            return await build_story_detail(
                self.database,
                story_id=story_id,
                public_site_url=self.settings.public_site_url,
            )

        self._health = HealthServer(
            self.settings.health_host,
            self.settings.health_port,
            health_status,
            story_feed_provider=public_story_feed,
            story_detail_provider=public_story_detail,
            cors_origins=self.settings.public_cors_origins,
        )
        await self._health.start()
        self.tree.add_command(NewsCommands(self))
        self.tree.add_command(
            app_commands.ContextMenu(
                name="Analyze Article",
                callback=self.analyze_article_message,
            )
        )
        self.tree.add_command(
            app_commands.ContextMenu(
                name="Fact Check",
                callback=self.fact_check_message,
            )
        )
        sync_guild = self.settings.guild_id or config.guild_id
        if sync_guild:
            guild = discord.Object(id=sync_guild)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden as exc:
                if exc.code != 50001:
                    raise
                self._command_sync_pending = True
                log.warning(
                    "Big cannot access configured guild %s yet; invite the bot to continue",
                    sync_guild,
                    extra={"event": "guild_access_pending", "guild_id": sync_guild},
                )
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
        if hasattr(self, "article_extractor"):
            await self.article_extractor.close()
        for task in self._maintenance_tasks:
            task.cancel()
        if self._maintenance_tasks:
            await asyncio.gather(*self._maintenance_tasks, return_exceptions=True)
        if self._health:
            await self._health.close()
        await self.database.close()
        await super().close()

    async def analyze_article_message(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if interaction.guild_id is None:
            await _send_notice(
                interaction,
                "Analyze Article",
                ("This command is available inside a server.",),
            )
            return
        urls = _message_article_urls(message)
        if not urls:
            await _send_notice(
                interaction,
                "Analyze Article",
                ("No supported article link was found in this message.",),
            )
            return
        if len(urls) > 1:
            await interaction.response.send_message(
                view=ArticleChoiceView(
                    bot=self,
                    user_id=interaction.user.id,
                    guild_id=interaction.guild_id,
                    message=message,
                    urls=urls,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _analyze_message_article(self, interaction, message, urls[0])

    async def fact_check_message(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        if interaction.guild_id is None:
            await _send_notice(
                interaction,
                "Fact Check",
                ("This command is available inside a server.",),
            )
            return
        if self.enricher is None:
            await _send_notice(
                interaction,
                "Fact Check",
                ("Fact checking is unavailable because OpenRouter is not configured.",),
            )
            return
        message_text = _message_fact_check_text(message)
        if not message_text:
            await _send_notice(
                interaction,
                "Fact Check",
                ("The selected message has no text to check.",),
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await _fact_check_message(self, interaction, message, message_text)

    async def on_ready(self) -> None:
        if self._scheduler is None:
            self._scheduler = asyncio.create_task(
                self.feed_service.run(), name="big-news-scheduler"
            )
            self.schedule_presentation_refresh()
        log.info("Big is ready", extra={"event": "ready"})
        if self.user:
            log.info("Invite Big with %s", invite_url(self.user.id), extra={"event": "invite"})

    def schedule_presentation_refresh(
        self, *, forum_channel_id: int | None = None, force: bool = False
    ) -> None:
        async def maintain_stories() -> None:
            await self.feed_service.refresh_presentation(
                forum_channel_id=forum_channel_id,
                force=force,
            )
            if forum_channel_id is None and not force:
                await self.feed_service.recover_pending_stories()
                await self.feed_service.recover_story_analysis()

        task = asyncio.create_task(
            maintain_stories(),
            name="big-presentation-refresh",
        )
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_finished)

    def _maintenance_finished(self, task: asyncio.Task[object]) -> None:
        self._maintenance_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error(
                "background maintenance failed",
                exc_info=(type(error), error, error.__traceback__),
                extra={"event": "maintenance_failed", "task": task.get_name()},
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        sync_guild = self.settings.guild_id or self.settings.app_config.guild_id
        if not self._command_sync_pending or guild.id != sync_guild:
            return
        target = discord.Object(id=guild.id)
        self.tree.copy_global_to(guild=target)
        await self.tree.sync(guild=target)
        self._command_sync_pending = False
        log.info(
            "Big commands synced after joining configured guild",
            extra={"event": "guild_commands_synced", "guild_id": guild.id},
        )


class NewsCommands(app_commands.Group):
    def __init__(self, bot: BigBot) -> None:
        super().__init__(name="news", description="Manage Big's forum news system")
        self.bot = bot

    @app_commands.command(name="status", description="Show news system status")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await _send_notice(interaction, "Status", await _status_lines(self.bot, interaction))

    @app_commands.command(name="feeds", description="List configured RSS and Atom feeds")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def feeds(self, interaction: discord.Interaction) -> None:
        view = await _make_feed_dashboard(self.bot, interaction)
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="settings", description="Configure story summaries")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def settings(self, interaction: discord.Interaction) -> None:
        guild_id = _guild_id(interaction)
        await interaction.response.send_message(
            view=AnalysisSettingsView(
                bot=self.bot,
                user_id=interaction.user.id,
                guild_id=guild_id,
                model=self.bot.feed_service.analysis_model(guild_id),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="tags", description="Check or install the recommended forum tags")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def tags(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=TagSetupView(
                bot=self.bot,
                user_id=interaction.user.id,
                guild_id=_guild_id(interaction),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="add-feed", description="Open the feed add form")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def add_feed(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=AddFeedForumView(
                bot=self.bot,
                user_id=interaction.user.id,
                guild_id=_guild_id(interaction),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="remove-feed", description="Open the feed removal panel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_feed(self, interaction: discord.Interaction) -> None:
        view = await _make_feed_dashboard(self.bot, interaction, mode="remove")
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="refresh", description="Open the manual refresh panel")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def refresh(self, interaction: discord.Interaction) -> None:
        view = await _make_feed_dashboard(self.bot, interaction, mode="refresh")
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="story", description="Open the story lookup form")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def story(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(StoryLookupModal(self.bot, _guild_id(interaction)))

    @app_commands.command(name="merge", description="Open the story merge form")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def merge(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MergeStoriesModal(self.bot, _guild_id(interaction)))

    @app_commands.command(name="split", description="Open the story split form")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def split(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(SplitArticleModal(self.bot, _guild_id(interaction)))

    @app_commands.command(name="reprocess", description="Open the article reprocess form")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def reprocess(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            ReprocessArticleModal(self.bot, _guild_id(interaction))
        )


ADMIN_COLOR = discord.Color.from_rgb(226, 91, 32)
MAX_FEED_OPTIONS = 25


class NoticeView(discord.ui.LayoutView):
    def __init__(self, title: str, lines: Sequence[str], *, timeout: float | None = 120) -> None:
        super().__init__(timeout=timeout)
        container: discord.ui.Container[NoticeView] = discord.ui.Container(accent_color=ADMIN_COLOR)
        container.add_item(discord.ui.TextDisplay(_panel_text(title, lines)))
        self.add_item(container)


class ArticleChoiceView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        bot: BigBot,
        user_id: int,
        guild_id: int,
        message: discord.Message,
        urls: Sequence[str],
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id
        self.guild_id = guild_id
        self.message = message
        self.urls = tuple(urls[:25])
        container: discord.ui.Container[ArticleChoiceView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(
            discord.ui.TextDisplay(
                _panel_text(
                    "Analyze Article",
                    ("Choose the article to analyze from this message.",),
                )
            )
        )
        container.add_item(discord.ui.ActionRow(ArticleUrlSelect(urls)))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await _send_notice(
            interaction,
            "Analyze Article",
            ("Open Analyze Article from the message to use your own picker.",),
        )
        return False


class ArticleUrlSelect(discord.ui.Select[ArticleChoiceView]):
    def __init__(self, urls: Sequence[str]) -> None:
        options = [
            discord.SelectOption(
                label=_url_option_label(url),
                value=str(index),
                description=_url_option_description(url),
            )
            for index, url in enumerate(urls[:25])
        ]
        super().__init__(
            placeholder="Choose article",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="big:article:choose",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        index = int(self.values[0])
        await _analyze_message_article(
            parent.bot,
            interaction,
            parent.message,
            parent.urls[index],
        )


class ArticleResultView(discord.ui.LayoutView):
    def __init__(
        self,
        result: ProcessedItem,
        *,
        public_site_url: str,
    ) -> None:
        super().__init__(timeout=None)
        container: discord.ui.Container[ArticleResultView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(discord.ui.TextDisplay(_article_result_text(result)))
        links: list[discord.ui.Button[ArticleResultView]] = []
        story_url = f"{public_site_url.rstrip('/')}/news/story/{result.story.id}/"
        links.append(discord.ui.Button(label="Open story", url=story_url))
        if result.story.discord_thread_id is not None:
            links.append(
                discord.ui.Button(
                    label="Open Forum post",
                    url=(
                        f"https://discord.com/channels/{result.story.guild_id}/"
                        f"{result.story.discord_thread_id}"
                    ),
                )
            )
        source_url = safe_external_link(result.article.url)
        if source_url:
            links.append(discord.ui.Button(label="Open article", url=source_url))
        container.add_item(discord.ui.ActionRow(*links))
        self.add_item(container)


class FactCheckResultView(discord.ui.LayoutView):
    def __init__(self, result: FactCheckResult) -> None:
        super().__init__(timeout=None)
        container: discord.ui.Container[FactCheckResultView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(discord.ui.TextDisplay(_fact_check_result_text(result)))
        self.add_item(container)


class OwnedLayoutView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        bot: BigBot,
        user_id: int,
        guild_id: int,
        timeout: float | None = 600,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user_id = user_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _send_notice(
                interaction,
                "Not your panel",
                ("Run `/news feeds` to open your own panel.",),
            )
            return False
        if not getattr(interaction.permissions, "manage_guild", False):
            await _send_notice(
                interaction,
                "No access",
                ("Manage Server is required for feed administration.",),
            )
            return False
        return True


class TagSetupView(OwnedLayoutView):
    def __init__(self, *, bot: BigBot, user_id: int, guild_id: int) -> None:
        super().__init__(bot=bot, user_id=user_id, guild_id=guild_id)
        container: discord.ui.Container[TagSetupView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(
            discord.ui.TextDisplay(
                _panel_text(
                    "Forum Tags",
                    (
                        "Choose the Forum Channel used for news.",
                        "Big will preserve existing tags and can add missing recommended tags.",
                    ),
                )
            )
        )
        container.add_item(discord.ui.ActionRow(TagForumSelect()))
        self.add_item(container)


class TagForumSelect(discord.ui.ChannelSelect[TagSetupView]):
    def __init__(self) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.forum],
            placeholder="Forum Channel",
            min_values=1,
            max_values=1,
            custom_id="big:tags:forum",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        selected = self.values[0]
        channel = parent.bot.get_channel(selected.id)
        if channel is None and interaction.guild is not None:
            try:
                channel = await interaction.guild.fetch_channel(selected.id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                channel = None
        if not isinstance(channel, discord.ForumChannel):
            await _send_notice(interaction, "Forum Tags", ("Select a Forum Channel.",))
            return
        await interaction.response.edit_message(
            view=TagAuditView(
                bot=parent.bot,
                user_id=parent.user_id,
                guild_id=parent.guild_id,
                forum=channel,
            )
        )


class TagAuditView(OwnedLayoutView):
    def __init__(
        self,
        *,
        bot: BigBot,
        user_id: int,
        guild_id: int,
        forum: discord.ForumChannel,
        notice: str | None = None,
    ) -> None:
        super().__init__(bot=bot, user_id=user_id, guild_id=guild_id)
        self.forum = forum
        existing = {tag.name.casefold() for tag in forum.available_tags}
        missing = tuple(tag for tag in TAG_CATALOG if tag.casefold() not in existing)
        free_slots = max(0, 20 - len(forum.available_tags))
        lines = [
            f"Forum: {forum.mention}",
            f"Recommended tags present: {len(TAG_CATALOG) - len(missing)}/{len(TAG_CATALOG)}",
            f"Free tag slots: {free_slots}",
        ]
        if notice:
            lines.insert(0, notice)
        if missing:
            lines.append(f"Missing: {', '.join(missing)}")
        else:
            lines.append("The complete tag set is installed.")
        if len(missing) > free_slots:
            lines.append("Remove unused forum tags before installing the missing set.")
        container: discord.ui.Container[TagAuditView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(discord.ui.TextDisplay(_panel_text("Forum Tags", lines)))
        container.add_item(
            discord.ui.ActionRow(
                InstallTagsButton(disabled=not missing or len(missing) > free_slots),
                ChooseAnotherTagForumButton(),
            )
        )
        self.add_item(container)


class InstallTagsButton(discord.ui.Button[TagAuditView]):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Install missing tags",
            style=discord.ButtonStyle.primary,
            custom_id="big:tags:install",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        forum = parent.forum
        existing = {tag.name.casefold() for tag in forum.available_tags}
        missing = tuple(tag for tag in TAG_CATALOG if tag.casefold() not in existing)
        if len(forum.available_tags) + len(missing) > 20:
            await interaction.response.edit_message(
                view=TagAuditView(
                    bot=parent.bot,
                    user_id=parent.user_id,
                    guild_id=parent.guild_id,
                    forum=forum,
                    notice="There are not enough free tag slots.",
                )
            )
            return
        try:
            updated = await forum.edit(
                available_tags=[
                    *forum.available_tags,
                    *(discord.ForumTag(name=name) for name in missing),
                ],
                reason=f"Big tag setup by {interaction.user.id}",
            )
        except discord.Forbidden:
            await _send_notice(
                interaction,
                "Forum Tags",
                ("Big needs Manage Channels in this forum to install tags.",),
            )
            return
        except discord.HTTPException:
            log.exception("forum tag installation failed", extra={"event": "tag_install_failed"})
            await _send_notice(
                interaction,
                "Forum Tags",
                ("Discord did not accept the tag update. Try again shortly.",),
            )
            return
        await interaction.response.edit_message(
            view=TagAuditView(
                bot=parent.bot,
                user_id=parent.user_id,
                guild_id=parent.guild_id,
                forum=updated or forum,
                notice=f"Installed {len(missing)} tag{'s' if len(missing) != 1 else ''}.",
            )
        )
        parent.bot.schedule_presentation_refresh(forum_channel_id=forum.id, force=True)


class ChooseAnotherTagForumButton(discord.ui.Button[TagAuditView]):
    def __init__(self) -> None:
        super().__init__(
            label="Choose another",
            style=discord.ButtonStyle.secondary,
            custom_id="big:tags:choose",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        await interaction.response.edit_message(
            view=TagSetupView(
                bot=parent.bot,
                user_id=parent.user_id,
                guild_id=parent.guild_id,
            )
        )


class FeedDashboardView(OwnedLayoutView):
    def __init__(
        self,
        *,
        bot: BigBot,
        user_id: int,
        guild_id: int,
        feeds: Sequence[Feed],
        selected: Feed | None = None,
        notice: str | None = None,
        mode: str = "manage",
    ) -> None:
        super().__init__(bot=bot, user_id=user_id, guild_id=guild_id)
        self.mode = mode
        self.feeds = tuple(feeds)
        self.selected = selected
        container: discord.ui.Container[FeedDashboardView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(discord.ui.TextDisplay(self._summary_text(notice)))
        control_row = discord.ui.ActionRow(
            AddFeedButton(),
            RefreshAllFeedsButton(disabled=not self.feeds),
        )
        container.add_item(control_row)
        if self.feeds:
            container.add_item(discord.ui.ActionRow(FeedSelect(self.feeds, selected)))
        if selected is not None:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(_feed_detail_text(selected)))
            container.add_item(
                discord.ui.ActionRow(
                    RefreshFeedButton(selected.id),
                    ToggleFeedButton(selected),
                    ToggleSummarizationButton(selected),
                    RemoveFeedButton(selected.id),
                )
            )
        self.add_item(container)

    def _summary_text(self, notice: str | None) -> str:
        active = sum(feed.state is FeedState.ACTIVE for feed in self.feeds)
        errors = sum(1 for feed in self.feeds if feed.last_error)
        retention = self.bot.settings.app_config.retention
        retention_text = (
            f"{retention.action} after {retention.clear_after_days} days"
            if retention.clear_after_days is not None
            else "off"
        )
        lines = [
            f"{active}/{len(self.feeds)} feeds active",
            f"Retention: {retention_text}",
            f"Errors: {errors}",
        ]
        if self.mode == "remove":
            lines.append("Select a feed, then use Remove.")
        elif self.mode == "refresh":
            lines.append("Refresh all feeds or select one feed.")
        elif not self.feeds:
            lines.append("Add a feed to begin creating forum stories.")
        if notice:
            lines.insert(0, notice)
        return _panel_text("Feeds", lines)


class AnalysisSettingsView(OwnedLayoutView):
    def __init__(
        self,
        *,
        bot: BigBot,
        user_id: int,
        guild_id: int,
        model: str | None,
        notice: str | None = None,
    ) -> None:
        super().__init__(bot=bot, user_id=user_id, guild_id=guild_id)
        self.model = model
        lines = [
            f"Model: `{_clean_text(model, 200)}`" if model else "OpenRouter is not configured.",
            "Summaries use the reporting stored with each story.",
            "Each feed controls whether summaries are enabled.",
        ]
        if notice:
            lines.insert(0, notice)
        container: discord.ui.Container[AnalysisSettingsView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(discord.ui.TextDisplay(_panel_text("Story Summaries", lines)))
        container.add_item(discord.ui.ActionRow(ChangeAnalysisModelButton(disabled=model is None)))
        self.add_item(container)


class ChangeAnalysisModelButton(discord.ui.Button[AnalysisSettingsView]):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Change model",
            style=discord.ButtonStyle.primary,
            custom_id="big:settings:model",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None or parent.model is None:
            return
        await interaction.response.send_modal(
            AnalysisModelModal(parent.bot, parent.guild_id, parent.model)
        )


class AnalysisModelModal(discord.ui.Modal):
    def __init__(self, bot: BigBot, guild_id: int, current_model: str) -> None:
        super().__init__(
            title="Story summary model",
            timeout=300,
            custom_id="big:settings:model_form",
        )
        self.bot = bot
        self.guild_id = guild_id
        self.model_input: discord.ui.TextInput[AnalysisModelModal] = discord.ui.TextInput(
            custom_id="model",
            default=current_model,
            placeholder="provider/model",
            min_length=3,
            max_length=200,
        )
        self.add_item(discord.ui.Label(text="OpenRouter model", component=self.model_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            model = await self.bot.feed_service.configure_analysis_model(
                guild_id=self.guild_id,
                model=self.model_input.value,
                actor_id=interaction.user.id,
            )
        except (EnrichmentError, ValueError) as exc:
            await _send_notice(interaction, "Story Summaries", (str(exc),))
            return
        await interaction.followup.send(
            view=AnalysisSettingsView(
                bot=self.bot,
                user_id=interaction.user.id,
                guild_id=self.guild_id,
                model=model,
                notice="Model saved.",
            ),
            ephemeral=True,
        )


class AddFeedButton(discord.ui.Button[FeedDashboardView]):
    def __init__(self) -> None:
        super().__init__(
            label="Add feed",
            style=discord.ButtonStyle.primary,
            custom_id="big:feed:add",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        await interaction.response.edit_message(
            view=AddFeedForumView(
                bot=parent.bot,
                user_id=parent.user_id,
                guild_id=parent.guild_id,
            )
        )


class RefreshAllFeedsButton(discord.ui.Button[FeedDashboardView]):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Refresh all",
            style=discord.ButtonStyle.secondary,
            custom_id="big:feed:refresh_all",
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        feeds = await parent.bot.database.list_feeds(parent.guild_id)
        reports: list[PollReport] = []
        failures = 0
        for feed in feeds:
            try:
                reports.append(await parent.bot.feed_service.poll_feed(feed.id))
            except Exception:
                failures += 1
                log.exception(
                    "manual feed refresh failed",
                    extra={"event": "manual_refresh_failed", "feed_id": feed.id},
                )
        total = _total_report(reports, failures=failures)
        await _send_notice(interaction, "Refresh", (_report_text(total),))


class FeedSelect(discord.ui.Select[FeedDashboardView]):
    def __init__(self, feeds: Sequence[Feed], selected: Feed | None) -> None:
        options = [
            discord.SelectOption(
                label=_select_label(feed),
                value=str(feed.id),
                description=_select_description(feed),
                default=selected is not None and selected.id == feed.id,
            )
            for feed in feeds[:MAX_FEED_OPTIONS]
        ]
        super().__init__(
            placeholder="Select feed",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="big:feed:select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        feed_id = _parse_positive_int(self.values[0], "feed id")
        view = await _make_feed_dashboard(
            parent.bot,
            interaction,
            selected_feed_id=feed_id,
            mode=parent.mode,
        )
        await interaction.response.edit_message(view=view)


class RefreshFeedButton(discord.ui.Button[FeedDashboardView]):
    def __init__(self, feed_id: int) -> None:
        super().__init__(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id=f"big:feed:{feed_id}:refresh",
        )
        self.feed_id = feed_id

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await parent.bot.feed_service.poll_feed(self.feed_id)
            notice = _report_text(report)
        except Exception as exc:
            log.exception(
                "manual feed refresh failed",
                extra={"event": "manual_refresh_failed", "feed_id": self.feed_id},
            )
            notice = f"Refresh failed: {_clean_text(str(exc), 160)}"
        view = await _make_feed_dashboard(
            parent.bot,
            interaction,
            selected_feed_id=self.feed_id,
            notice=notice,
            mode=parent.mode,
        )
        await interaction.followup.send(view=view, ephemeral=True)


class ToggleFeedButton(discord.ui.Button[FeedDashboardView]):
    def __init__(self, feed: Feed) -> None:
        self.feed_id = feed.id
        self.next_state = FeedState.PAUSED if feed.state is FeedState.ACTIVE else FeedState.ACTIVE
        super().__init__(
            label="Pause" if self.next_state is FeedState.PAUSED else "Resume",
            style=discord.ButtonStyle.secondary,
            custom_id=f"big:feed:{feed.id}:toggle",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        changed = await parent.bot.database.set_feed_state(self.feed_id, self.next_state)
        if changed:
            await parent.bot.database.audit(
                guild_id=parent.guild_id,
                actor_id=interaction.user.id,
                action="news.feed_state",
                subject=str(self.feed_id),
                detail={"state": self.next_state.value},
            )
        notice = f"Feed {self.next_state.value}." if changed else "Feed was not found."
        view = await _make_feed_dashboard(
            parent.bot,
            interaction,
            selected_feed_id=self.feed_id,
            notice=notice,
            mode=parent.mode,
        )
        await interaction.response.edit_message(view=view)


class ToggleSummarizationButton(discord.ui.Button[FeedDashboardView]):
    def __init__(self, feed: Feed) -> None:
        self.feed_id = feed.id
        self.enabled = not feed.summarization_enabled
        super().__init__(
            label="Summaries off" if feed.summarization_enabled else "Summaries on",
            style=discord.ButtonStyle.secondary,
            custom_id=f"big:feed:{feed.id}:summaries",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        changed = await parent.bot.database.set_feed_summarization(
            self.feed_id,
            enabled=self.enabled,
        )
        if changed:
            await parent.bot.database.audit(
                guild_id=parent.guild_id,
                actor_id=interaction.user.id,
                action="news.feed_summarization",
                subject=str(self.feed_id),
                detail={"enabled": self.enabled},
            )
        state = "enabled" if self.enabled else "disabled"
        notice = (
            f"Summaries {state}. This applies when a story is next created or updated."
            if changed
            else "Feed was not found."
        )
        view = await _make_feed_dashboard(
            parent.bot,
            interaction,
            selected_feed_id=self.feed_id,
            notice=notice,
            mode=parent.mode,
        )
        await interaction.response.edit_message(view=view)


class RemoveFeedButton(discord.ui.Button[FeedDashboardView]):
    def __init__(self, feed_id: int) -> None:
        super().__init__(
            label="Remove",
            style=discord.ButtonStyle.danger,
            custom_id=f"big:feed:{feed_id}:remove",
        )
        self.feed_id = feed_id

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        feed = await parent.bot.database.get_feed(self.feed_id)
        if feed is None or feed.guild_id != parent.guild_id:
            view = await _make_feed_dashboard(parent.bot, interaction, notice="Feed was not found.")
            await interaction.response.edit_message(view=view)
            return
        await interaction.response.edit_message(
            view=ConfirmRemoveFeedView(
                bot=parent.bot,
                user_id=parent.user_id,
                guild_id=parent.guild_id,
                feed=feed,
            )
        )


class ConfirmRemoveFeedView(OwnedLayoutView):
    def __init__(self, *, bot: BigBot, user_id: int, guild_id: int, feed: Feed) -> None:
        super().__init__(bot=bot, user_id=user_id, guild_id=guild_id)
        self.feed = feed
        container: discord.ui.Container[ConfirmRemoveFeedView] = discord.ui.Container(
            accent_color=discord.Color.red()
        )
        container.add_item(
            discord.ui.TextDisplay(
                _panel_text(
                    "Remove Feed",
                    (
                        f"`{feed.id}` {_clean_text(feed.name, 80)}",
                        "Stored story history stays in SQLite.",
                    ),
                )
            )
        )
        container.add_item(discord.ui.ActionRow(ConfirmRemoveButton(feed.id), CancelFeedButton()))
        self.add_item(container)


class ConfirmRemoveButton(discord.ui.Button[ConfirmRemoveFeedView]):
    def __init__(self, feed_id: int) -> None:
        super().__init__(
            label="Confirm remove",
            style=discord.ButtonStyle.danger,
            custom_id=f"big:feed:{feed_id}:confirm_remove",
        )
        self.feed_id = feed_id

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        removed = await parent.bot.database.remove_feed(self.feed_id)
        if removed:
            await parent.bot.database.audit(
                guild_id=parent.guild_id,
                actor_id=interaction.user.id,
                action="news.remove_feed",
                subject=str(self.feed_id),
            )
        notice = "Feed removed." if removed else "Feed was already gone."
        view = await _make_feed_dashboard(parent.bot, interaction, notice=notice)
        await interaction.response.edit_message(view=view)


class CancelFeedButton(discord.ui.Button[ConfirmRemoveFeedView]):
    def __init__(self) -> None:
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="big:feed:cancel_remove",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        view = await _make_feed_dashboard(
            parent.bot,
            interaction,
            selected_feed_id=parent.feed.id,
            notice="No changes made.",
        )
        await interaction.response.edit_message(view=view)


class AddFeedForumView(OwnedLayoutView):
    def __init__(self, *, bot: BigBot, user_id: int, guild_id: int) -> None:
        super().__init__(bot=bot, user_id=user_id, guild_id=guild_id)
        container: discord.ui.Container[AddFeedForumView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(
            discord.ui.TextDisplay(
                _panel_text(
                    "Add Feed",
                    (
                        "Choose the destination Forum Channel.",
                        "Every story created from this feed will become a forum post.",
                    ),
                )
            )
        )
        container.add_item(discord.ui.ActionRow(ForumChannelSelect()))
        container.add_item(discord.ui.ActionRow(CancelAddFeedButton()))
        self.add_item(container)


class ForumChannelSelect(discord.ui.ChannelSelect[AddFeedForumView]):
    def __init__(self) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.forum],
            placeholder="Forum Channel",
            min_values=1,
            max_values=1,
            custom_id="big:feed:forum",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        selected = self.values[0]
        channel = parent.bot.get_channel(selected.id)
        if channel is None and interaction.guild is not None:
            try:
                channel = await interaction.guild.fetch_channel(selected.id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                channel = None
        if not isinstance(channel, discord.ForumChannel):
            await _send_notice(interaction, "Add Feed", ("Select a Forum Channel.",))
            return
        await interaction.response.edit_message(
            view=AddFeedSummaryView(
                bot=parent.bot,
                user_id=parent.user_id,
                guild_id=parent.guild_id,
                forum=channel,
            )
        )


class AddFeedSummaryView(OwnedLayoutView):
    def __init__(
        self,
        *,
        bot: BigBot,
        user_id: int,
        guild_id: int,
        forum: discord.ForumChannel,
    ) -> None:
        super().__init__(bot=bot, user_id=user_id, guild_id=guild_id)
        self.forum = forum
        container: discord.ui.Container[AddFeedSummaryView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(
            discord.ui.TextDisplay(
                _panel_text(
                    "Story Summaries",
                    (
                        f"Forum: {forum.mention}",
                        "Choose whether stories from this feed receive a factual source summary.",
                    ),
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                AddFeedSummaryChoiceButton(enabled=True),
                AddFeedSummaryChoiceButton(enabled=False),
            )
        )
        container.add_item(discord.ui.ActionRow(CancelAddFeedButton()))
        self.add_item(container)


class AddFeedSummaryChoiceButton(discord.ui.Button[AddFeedSummaryView]):
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        super().__init__(
            label="Summaries on" if enabled else "Summaries off",
            style=discord.ButtonStyle.primary if enabled else discord.ButtonStyle.secondary,
            custom_id=f"big:feed:summary:{'on' if enabled else 'off'}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        await interaction.response.send_modal(
            AddFeedModal(
                parent.bot,
                parent.guild_id,
                parent.forum,
                summarization_enabled=self.enabled,
            )
        )


class CancelAddFeedButton(discord.ui.Button[AddFeedForumView]):
    def __init__(self) -> None:
        super().__init__(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="big:feed:cancel_add",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if parent is None:
            return
        view = await _make_feed_dashboard(parent.bot, interaction, notice="No changes made.")
        await interaction.response.edit_message(view=view)


class AddFeedModal(discord.ui.Modal):
    def __init__(
        self,
        bot: BigBot,
        guild_id: int,
        forum: discord.ForumChannel,
        *,
        summarization_enabled: bool,
    ) -> None:
        super().__init__(title="Add feed", timeout=300, custom_id="big:feed:add_modal")
        self.bot = bot
        self.guild_id = guild_id
        self.forum = forum
        self.summarization_enabled = summarization_enabled
        self.name_input: discord.ui.TextInput[AddFeedModal] = discord.ui.TextInput(
            custom_id="name",
            placeholder="Reuters Markets",
            min_length=1,
            max_length=50,
        )
        self.publisher_input: discord.ui.TextInput[AddFeedModal] = discord.ui.TextInput(
            custom_id="publisher",
            placeholder="Reuters",
            required=False,
            max_length=80,
        )
        self.url_input: discord.ui.TextInput[AddFeedModal] = discord.ui.TextInput(
            custom_id="url",
            placeholder="https://example.com/feed.xml",
            min_length=8,
            max_length=500,
        )
        self.interval_input: discord.ui.TextInput[AddFeedModal] = discord.ui.TextInput(
            custom_id="interval",
            placeholder="15",
            default="15",
            min_length=1,
            max_length=4,
        )
        self.tags_input: discord.ui.TextInput[AddFeedModal] = discord.ui.TextInput(
            custom_id="tags",
            placeholder="Markets, Breaking",
            required=False,
            max_length=200,
        )
        self.add_item(discord.ui.Label(text="Feed name", component=self.name_input))
        self.add_item(discord.ui.Label(text="Publisher", component=self.publisher_input))
        self.add_item(discord.ui.Label(text="RSS or Atom URL", component=self.url_input))
        self.add_item(discord.ui.Label(text="Poll minutes", component=self.interval_input))
        self.add_item(discord.ui.Label(text="Forum tags", component=self.tags_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = self.name_input.value.strip()
        publisher = self.publisher_input.value.strip() or name
        url = self.url_input.value.strip()
        try:
            _validate_name(name)
            interval = _parse_interval_minutes(self.interval_input.value)
            await validate_feed_url(url)
            tags = _parse_tag_names(self.tags_input.value, self.forum)
            feed = await self.bot.database.add_feed(
                guild_id=self.guild_id,
                forum_channel_id=self.forum.id,
                name=name,
                kind=FeedKind.RSS,
                source=url,
                publisher=publisher,
                interval_seconds=interval * 60,
                tag_ids=(),
                default_tags=tags,
                include_replies=False,
                include_reposts=False,
                created_by=interaction.user.id,
                summarization_enabled=self.summarization_enabled,
            )
        except DuplicateFeedError as exc:
            await _send_notice(interaction, "Add Feed", (str(exc),))
            return
        except ValueError as exc:
            await _send_notice(interaction, "Add Feed", (str(exc),))
            return
        await self.bot.database.audit(
            guild_id=self.guild_id,
            actor_id=interaction.user.id,
            action="news.add_feed",
            subject=str(feed.id),
            detail={
                "forum_channel_id": self.forum.id,
                "url": url,
                "summarization_enabled": self.summarization_enabled,
            },
        )
        view = await _make_feed_dashboard(
            self.bot,
            interaction,
            selected_feed_id=feed.id,
            notice=f"Added `{feed.id}` {_clean_text(feed.name, 80)}.",
        )
        await interaction.followup.send(view=view, ephemeral=True)


class StoryLookupModal(discord.ui.Modal):
    def __init__(self, bot: BigBot, guild_id: int) -> None:
        super().__init__(title="Find story", timeout=300, custom_id="big:story:lookup")
        self.bot = bot
        self.guild_id = guild_id
        self.story_id: discord.ui.TextInput[StoryLookupModal] = discord.ui.TextInput(
            custom_id="story_id",
            placeholder="123",
            min_length=1,
            max_length=20,
        )
        self.add_item(discord.ui.Label(text="Story ID", component=self.story_id))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            story_id = _parse_positive_int(self.story_id.value, "story id")
        except ValueError as exc:
            await _send_notice(interaction, "Story", (str(exc),))
            return
        story = await self.bot.database.get_story(story_id)
        if story is None or story.guild_id != self.guild_id:
            await _send_notice(interaction, "Story", ("Story not found.",))
            return
        articles = await self.bot.database.story_articles(story.id)
        lines = [
            f"`{story.id}` | {story.state.value} | {story.publication_state.value}",
            _clean_text(story.title, 180),
            f"Sources: {len(articles)}",
        ]
        lines.extend(
            f"`{article.id}` {_clean_text(article.publisher, 80)} | {article.url}"
            for article in articles[:10]
        )
        await _send_notice(interaction, "Story", lines)


class MergeStoriesModal(discord.ui.Modal):
    def __init__(self, bot: BigBot, guild_id: int) -> None:
        super().__init__(title="Merge stories", timeout=300, custom_id="big:story:merge")
        self.bot = bot
        self.guild_id = guild_id
        self.target_id: discord.ui.TextInput[MergeStoriesModal] = discord.ui.TextInput(
            custom_id="target_story_id",
            placeholder="Main story ID",
            min_length=1,
            max_length=20,
        )
        self.source_id: discord.ui.TextInput[MergeStoriesModal] = discord.ui.TextInput(
            custom_id="source_story_id",
            placeholder="Story ID to merge into main",
            min_length=1,
            max_length=20,
        )
        self.add_item(discord.ui.Label(text="Main story ID", component=self.target_id))
        self.add_item(discord.ui.Label(text="Story to merge", component=self.source_id))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target_id = _parse_positive_int(self.target_id.value, "main story id")
            source_id = _parse_positive_int(self.source_id.value, "story to merge")
        except ValueError as exc:
            await _send_notice(interaction, "Merge", (str(exc),))
            return
        target = await self.bot.database.get_story(target_id)
        if target is None or target.guild_id != self.guild_id:
            await _send_notice(interaction, "Merge", ("Main story not found.",))
            return
        try:
            merged = await self.bot.feed_service.merge_stories(
                target_id,
                source_id,
                actor_id=interaction.user.id,
            )
        except ValueError as exc:
            await _send_notice(interaction, "Merge", (str(exc),))
            return
        await _send_notice(interaction, "Merge", (f"Merged `{source_id}` into `{merged.id}`.",))


class SplitArticleModal(discord.ui.Modal):
    def __init__(self, bot: BigBot, guild_id: int) -> None:
        super().__init__(title="Split article", timeout=300, custom_id="big:story:split")
        self.bot = bot
        self.guild_id = guild_id
        self.article_id: discord.ui.TextInput[SplitArticleModal] = discord.ui.TextInput(
            custom_id="article_id",
            placeholder="456",
            min_length=1,
            max_length=20,
        )
        self.add_item(discord.ui.Label(text="Article ID", component=self.article_id))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            article_id = _parse_positive_int(self.article_id.value, "article id")
        except ValueError as exc:
            await _send_notice(interaction, "Split", (str(exc),))
            return
        article = await self.bot.database.get_article(article_id)
        if article is None or article.story_id is None:
            await _send_notice(interaction, "Split", ("Article not found.",))
            return
        story = await self.bot.database.get_story(article.story_id)
        if story is None or story.guild_id != self.guild_id:
            await _send_notice(interaction, "Split", ("Article not found.",))
            return
        try:
            new_story = await self.bot.feed_service.split_article(
                article_id,
                actor_id=interaction.user.id,
            )
        except ValueError as exc:
            await _send_notice(interaction, "Split", (str(exc),))
            return
        await _send_notice(
            interaction,
            "Split",
            (f"Moved article `{article_id}` into story `{new_story.id}`.",),
        )


class ReprocessArticleModal(discord.ui.Modal):
    def __init__(self, bot: BigBot, guild_id: int) -> None:
        super().__init__(title="Reprocess article", timeout=300, custom_id="big:story:reprocess")
        self.bot = bot
        self.guild_id = guild_id
        self.article_id: discord.ui.TextInput[ReprocessArticleModal] = discord.ui.TextInput(
            custom_id="article_id",
            placeholder="456",
            min_length=1,
            max_length=20,
        )
        self.add_item(discord.ui.Label(text="Article ID", component=self.article_id))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            article_id = _parse_positive_int(self.article_id.value, "article id")
        except ValueError as exc:
            await _send_notice(interaction, "Reprocess", (str(exc),))
            return
        article = await self.bot.database.get_article(article_id)
        if article is None or article.story_id is None:
            await _send_notice(interaction, "Reprocess", ("Article not found.",))
            return
        story = await self.bot.database.get_story(article.story_id)
        if story is None or story.guild_id != self.guild_id:
            await _send_notice(interaction, "Reprocess", ("Article not found.",))
            return
        try:
            result = await self.bot.feed_service.reprocess_article(article_id)
        except ValueError as exc:
            await _send_notice(interaction, "Reprocess", (str(exc),))
            return
        await _send_notice(interaction, "Reprocess", (f"Result: {result}.",))


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
            summarization_enabled=spec.summarization_enabled,
        )


def _guild_id(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise app_commands.NoPrivateMessage()
    return interaction.guild_id


def _message_article_urls(message: discord.Message) -> tuple[str, ...]:
    return extract_article_urls(_message_text_values(message, include_embed_urls=True))


def _message_fact_check_text(message: discord.Message) -> str:
    values = _message_text_values(message, include_embed_urls=True)
    return "\n".join(value.strip() for value in values if value.strip())[:8000]


def _message_text_values(message: discord.Message, *, include_embed_urls: bool) -> list[str]:
    values: list[str] = [message.content]
    for embed in message.embeds:
        embed_values = [embed.title, embed.description, embed.author.name]
        if include_embed_urls:
            embed_values.extend((embed.url, embed.author.url))
        for value in embed_values:
            if value:
                values.append(str(value))
        for field in embed.fields:
            if field.name:
                values.append(str(field.name))
            if field.value:
                values.append(str(field.value))
    return values


async def _fact_check_message(
    bot: BigBot,
    interaction: discord.Interaction,
    message: discord.Message,
    message_text: str,
) -> None:
    if bot.enricher is None or interaction.guild_id is None:
        await interaction.edit_original_response(
            view=NoticeView("Fact Check", ("Fact checking is not configured.",))
        )
        return
    try:
        result = await bot.enricher.fact_check(
            guild_id=interaction.guild_id,
            message_text=message_text,
            message_urls=extract_article_urls((message_text,)),
        )
        await message.reply(
            view=FactCheckResultView(result),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        checked = len(result.claims)
        label = "claim" if checked == 1 else "claims"
        await interaction.edit_original_response(
            view=NoticeView(
                "Fact Check",
                (f"Checked {checked} verifiable {label} and replied to the message.",),
            )
        )
        log.info(
            "message fact check completed",
            extra={
                "event": "message_fact_check_completed",
                "guild_id": interaction.guild_id,
                "user_id": interaction.user.id,
                "message_id": message.id,
                "claim_count": checked,
            },
        )
    except EnrichmentError as exc:
        log.warning(
            "message fact check failed",
            extra={
                "event": "message_fact_check_failed",
                "message_id": message.id,
                "error_type": type(exc).__name__,
            },
        )
        await interaction.edit_original_response(
            view=NoticeView("Fact Check", (plain_text(str(exc), limit=400),))
        )
    except discord.Forbidden:
        await interaction.edit_original_response(
            view=NoticeView(
                "Fact Check",
                ("Big cannot reply to that message. Check its channel permissions.",),
            )
        )
    except discord.HTTPException:
        log.exception(
            "fact-check reply failed",
            extra={"event": "fact_check_reply_failed", "message_id": message.id},
        )
        await interaction.edit_original_response(
            view=NoticeView("Fact Check", ("Discord rejected the fact-check result.",))
        )


def _fact_check_claim_text(index: int, claim: FactCheckClaim) -> str:
    sections = [
        f"### {index}. {claim.verdict.value}",
        f"**Claim:** {plain_text(claim.claim, limit=280)}",
        plain_text(claim.explanation, limit=420),
    ]
    if claim.sources:
        links: list[str] = []
        for source in claim.sources[:3]:
            link = f"[{source.label}]({source.url})"
            if len(" · ".join((*links, link))) > 650:
                break
            links.append(link)
        if links:
            sections.append(f"**Evidence:** {' · '.join(links)}")
        else:
            sections.append("**Evidence:** Reliable sources were found, but their links are long.")
    else:
        sections.append("**Evidence:** No reliable source established or refuted this claim.")
    return "\n\n".join(sections)


def _fact_check_result_text(result: FactCheckResult) -> str:
    if not result.claims:
        return "## Fact Check\n\nNo objectively verifiable factual claim was found in this message."
    claims = [_fact_check_claim_text(index, claim) for index, claim in enumerate(result.claims, 1)]
    text = "## Fact Check\n\n" + "\n\n".join(claims)
    return text if len(text) <= 3900 else text[:3897].rstrip() + "..."


async def _analyze_message_article(
    bot: BigBot,
    interaction: discord.Interaction,
    message: discord.Message,
    url: str,
) -> None:
    try:
        feed = await _analysis_feed(bot, interaction.guild_id, message, url)
        item = await bot.article_extractor.fetch(url)
        result = await bot.feed_service.process_external_item(feed, item)
        await message.reply(
            view=ArticleResultView(result, public_site_url=bot.settings.public_site_url),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.edit_original_response(
            view=NoticeView(
                "Analyze Article",
                (_analysis_completion_text(result),),
            )
        )
        log.info(
            "message article analyzed",
            extra={
                "event": "message_article_analyzed",
                "guild_id": interaction.guild_id,
                "user_id": interaction.user.id,
                "message_id": message.id,
                "story_id": result.story.id,
                "outcome": result.outcome,
            },
        )
    except (ArticleExtractionError, ValueError) as exc:
        await interaction.edit_original_response(
            view=NoticeView("Analyze Article", (plain_text(str(exc), limit=400),))
        )
    except discord.Forbidden:
        await interaction.edit_original_response(
            view=NoticeView(
                "Analyze Article",
                ("Big cannot reply to that message. Check its channel permissions.",),
            )
        )
    except discord.HTTPException:
        log.exception(
            "message article reply failed",
            extra={"event": "message_article_reply_failed", "message_id": message.id},
        )
        await interaction.edit_original_response(
            view=NoticeView(
                "Analyze Article",
                ("Discord rejected the result. Nothing was posted twice.",),
            )
        )
    except Exception:
        log.exception(
            "message article analysis failed",
            extra={"event": "message_article_analysis_failed", "message_id": message.id},
        )
        await interaction.edit_original_response(
            view=NoticeView(
                "Analyze Article",
                ("The article could not be analyzed. The failure was recorded safely.",),
            )
        )


async def _analysis_feed(
    bot: BigBot,
    guild_id: int | None,
    message: discord.Message,
    url: str,
) -> Feed:
    if guild_id is None:
        raise ValueError("This command is available inside a server.")
    feeds = [
        feed for feed in await bot.database.list_feeds(guild_id) if feed.state is FeedState.ACTIVE
    ]
    if not feeds:
        raise ValueError("No active Forum feed is configured for this server.")
    parent_id = getattr(message.channel, "parent_id", None)
    if parent_id is not None:
        matching_forum = [feed for feed in feeds if feed.forum_channel_id == parent_id]
        if matching_forum:
            return matching_forum[0]
    hostname = (urlsplit(url).hostname or "").casefold().removeprefix("www.")
    for feed in feeds:
        source_host = (urlsplit(feed.source).hostname or "").casefold().removeprefix("www.")
        if hostname and hostname == source_host:
            return feed
    return feeds[0]


def _article_result_text(result: ProcessedItem) -> str:
    story = result.story
    display = analysis_display(story.analysis or "") if story.analysis else None
    analysis = display.body if display and display.body else _fallback_story_text(story)
    sections = [f"## {forum_title(story.title)}", analysis]
    reporting: list[tuple[str, str]] = []
    if display:
        reporting.extend(display.sources)
    article_url = safe_external_link(result.article.url)
    article_source = (plain_text(result.article.publisher, limit=80), article_url)
    if article_url and article_source not in reporting:
        reporting.insert(0, article_source)
    if reporting:
        sections.extend(
            (
                "**Related reporting**",
                "\n".join(f"- [{label}]({url})" for label, url in reporting[:6]),
            )
        )
    if result.related_stories:
        links = [
            _related_story_link(candidate)
            for candidate in result.related_stories[:4]
            if candidate.discord_thread_id is not None
        ]
        if links:
            sections.extend(("**Related stories**", "\n".join(f"- {link}" for link in links)))
    text = "\n\n".join(section for section in sections if section).strip()
    return text if len(text) <= 3900 else text[:3897].rstrip() + "..."


def _fallback_story_text(story: Story) -> str:
    summary = plain_text(story.summary, limit=900)
    return f"**Summary**\n\n{summary}"


def _related_story_link(story: Story) -> str:
    title = forum_title(story.title)
    url = f"https://discord.com/channels/{story.guild_id}/{story.discord_thread_id}"
    return f"[{title}]({url})"


def _analysis_completion_text(result: ProcessedItem) -> str:
    if result.outcome == "duplicates":
        return f"Already covered in Story {result.story.id}. The existing analysis was linked."
    if result.outcome in {"failed", "uncertain"}:
        return (
            f"Saved as Story {result.story.id}, but the Forum post could not be confirmed. "
            "The article was not submitted twice."
        )
    return f"Added to Story {result.story.id} and replied to the selected message."


def _url_option_label(url: str) -> str:
    parts = urlsplit(url)
    hostname = (parts.hostname or "Article").removeprefix("www.")
    path = parts.path.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    label = f"{hostname}: {path}" if path else hostname
    return plain_text(label, limit=100)


def _url_option_description(url: str) -> str:
    parts = urlsplit(url)
    value = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return plain_text(value, limit=100)


async def _status_lines(bot: BigBot, interaction: discord.Interaction) -> tuple[str, ...]:
    guild_id = _guild_id(interaction)
    feeds = await bot.database.list_feeds(guild_id)
    counts = await bot.database.story_counts(guild_id)
    active = sum(feed.state is FeedState.ACTIVE for feed in feeds)
    errors = [feed for feed in feeds if feed.last_error]
    retention = bot.settings.app_config.retention
    retention_text = (
        f"{retention.action} after {retention.clear_after_days} days"
        if retention.clear_after_days is not None
        else "off"
    )
    return (
        f"{active}/{len(feeds)} feeds active",
        (
            f"{sum(counts.values())} stories, {counts['breaking']} breaking, "
            f"{counts['developing']} developing, {counts['updated']} updated"
        ),
        f"Retention: {retention_text}",
        f"Feed errors: {len(errors)}",
    )


async def _make_feed_dashboard(
    bot: BigBot,
    interaction: discord.Interaction,
    *,
    selected_feed_id: int | None = None,
    notice: str | None = None,
    mode: str = "manage",
) -> FeedDashboardView:
    guild_id = _guild_id(interaction)
    feeds = await bot.database.list_feeds(guild_id)
    selected = None
    if selected_feed_id is not None:
        selected = next((feed for feed in feeds if feed.id == selected_feed_id), None)
    return FeedDashboardView(
        bot=bot,
        user_id=interaction.user.id,
        guild_id=guild_id,
        feeds=feeds,
        selected=selected,
        notice=notice,
        mode=mode,
    )


async def _send_notice(
    interaction: discord.Interaction,
    title: str,
    lines: Sequence[str],
) -> None:
    view = NoticeView(title, tuple(lines))
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


def _validate_name(value: str) -> None:
    if not FEED_NAME.fullmatch(value.strip()):
        raise ValueError("feed name must be 1 to 50 simple characters")


def _parse_interval_minutes(value: str) -> int:
    minutes = _parse_positive_int(value, "poll minutes")
    if not 5 <= minutes <= 1440:
        raise ValueError("poll minutes must be between 5 and 1440")
    return minutes


def _parse_positive_int(value: str, label: str) -> int:
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


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


def _panel_text(title: str, lines: Sequence[str]) -> str:
    body = "\n".join(_clean_text(line, 300) for line in lines if line)
    return f"**{_clean_text(title, 80)}**\n{body}"[:4000]


def _feed_detail_text(feed: Feed) -> str:
    tags = ", ".join(feed.default_tags) or "automatic tags"
    last_polled = feed.last_polled_at.isoformat() if feed.last_polled_at else "not yet"
    lines = [
        f"`{feed.id}` {_clean_text(feed.name, 80)}",
        f"Forum: <#{feed.forum_channel_id}>",
        f"Publisher: {_clean_text(feed.publisher or feed.name, 80)}",
        f"State: {feed.state.value}",
        f"Summaries: {'on' if feed.summarization_enabled else 'off'}",
        f"Tags: {tags}",
        f"Interval: {feed.interval_seconds // 60} minutes",
        f"Last poll: {last_polled}",
    ]
    if feed.last_error:
        lines.append(f"Error: {_clean_text(feed.last_error, 180)}")
    return _panel_text("Selected Feed", lines)


def _select_label(feed: Feed) -> str:
    suffix = "paused" if feed.state is FeedState.PAUSED else "active"
    return _clean_text(f"{feed.id} | {feed.name} | {suffix}", 100)


def _select_description(feed: Feed) -> str:
    tags = ", ".join(feed.default_tags) or "automatic tags"
    summaries = "summaries on" if feed.summarization_enabled else "summaries off"
    return _clean_text(f"{feed.interval_seconds // 60} min | {summaries} | {tags}", 100)


def _clean_text(value: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", value.replace("\u2014", "-").replace("\u2013", "-")).strip()
    return clean[:limit].rstrip()


def _total_report(reports: Sequence[PollReport], *, failures: int = 0) -> PollReport:
    return PollReport(
        fetched=sum(report.fetched for report in reports),
        new_stories=sum(report.new_stories for report in reports),
        updated_stories=sum(report.updated_stories for report in reports),
        duplicates=sum(report.duplicates for report in reports),
        skipped=sum(report.skipped for report in reports),
        failed=sum(report.failed for report in reports) + failures,
        uncertain=sum(report.uncertain for report in reports),
    )


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
