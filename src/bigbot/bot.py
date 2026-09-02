from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from bigbot.classification import StoryClassifier
from bigbot.clustering import DeterministicClusterer
from bigbot.config import Settings
from bigbot.database import Database, DuplicateFeedError
from bigbot.domain import Feed, FeedKind, FeedState
from bigbot.enrichment import EnrichmentError, build_story_analyzer
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
            retention_after_days=config.retention.clear_after_days,
            retention_action=config.retention.action,
            retention_batch_size=config.retention.batch_size,
            analyzer=build_story_analyzer(self.settings, model_overrides=model_overrides),
            related_story_limit=self.settings.related_story_limit,
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

    @app_commands.command(name="settings", description="Configure story analysis")
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
            f"Web grounding: {'on' if bot.settings.ai_web_search else 'off'}",
        ]
        if notice:
            lines.insert(0, notice)
        container: discord.ui.Container[AnalysisSettingsView] = discord.ui.Container(
            accent_color=ADMIN_COLOR
        )
        container.add_item(discord.ui.TextDisplay(_panel_text("Story Analysis", lines)))
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
            title="Story analysis model",
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
            await _send_notice(interaction, "Story Analysis", (str(exc),))
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
        await interaction.response.send_modal(AddFeedModal(parent.bot, parent.guild_id, channel))


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
    def __init__(self, bot: BigBot, guild_id: int, forum: discord.ForumChannel) -> None:
        super().__init__(title="Add feed", timeout=300, custom_id="big:feed:add_modal")
        self.bot = bot
        self.guild_id = guild_id
        self.forum = forum
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
            detail={"forum_channel_id": self.forum.id, "url": url},
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
        )


def _guild_id(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise app_commands.NoPrivateMessage()
    return interaction.guild_id


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
    return _clean_text(f"{feed.interval_seconds // 60} min | {tags}", 100)


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
