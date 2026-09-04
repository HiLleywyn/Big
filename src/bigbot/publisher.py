from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import discord

from bigbot.analysis_format import analysis_display, story_update_detail, visible_story_updates
from bigbot.domain import AnalysisState, Article, Feed, PublishReceipt, Story, StoryUpdate
from bigbot.security import (
    forum_title,
    neutralize_mentions,
    plain_text,
    publisher_label,
    safe_external_link,
)

log = logging.getLogger(__name__)
BRAND_ICON_FILENAME = "big-feed-mark.jpg"
BRAND_ICON_URI = f"attachment://{BRAND_ICON_FILENAME}"
BRAND_ICON_PATH = Path(__file__).with_name("assets") / BRAND_ICON_FILENAME


class PublishError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = True) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class ForumPublisher(Protocol):
    async def create_story(
        self,
        feed: Feed,
        story: Story,
        articles: list[Article],
        related_stories: list[Story],
        updates: list[StoryUpdate],
    ) -> PublishReceipt: ...

    async def update_story(
        self,
        story: Story,
        articles: list[Article],
        article: Article,
        related_stories: list[Story],
        updates: list[StoryUpdate],
        *,
        post_update: bool,
    ) -> int | None: ...

    async def mark_merged(self, source: Story, target: Story) -> None: ...

    async def archive_story(self, story: Story) -> None: ...

    async def delete_story(self, story: Story) -> None: ...


class DiscordForumPublisher:
    def __init__(
        self, client: discord.Client, *, public_site_url: str = "https://bigif.org"
    ) -> None:
        self._client = client
        self._public_site_url = public_site_url.rstrip("/")

    async def create_story(
        self,
        feed: Feed,
        story: Story,
        articles: list[Article],
        related_stories: list[Story],
        updates: list[StoryUpdate],
    ) -> PublishReceipt:
        channel = await self._forum(feed.forum_channel_id, feed.guild_id)
        tags = self._resolve_tags(channel, story.tags, feed.tag_ids)
        try:
            icon = _brand_icon_file()
            try:
                result = await channel.create_thread(
                    name=forum_title(story.title),
                    content=neutralize_mentions(_starter_content(story)),
                    embed=_story_embed(
                        story,
                        articles,
                        related_stories,
                        updates,
                        public_site_url=self._public_site_url,
                    ),
                    file=icon,
                    applied_tags=tags,
                    auto_archive_duration=1440,
                    allowed_mentions=discord.AllowedMentions.none(),
                    reason=f"Big story {story.id}",
                )
            finally:
                icon.close()
        except discord.HTTPException as exc:
            raise _publish_error("create forum story", exc) from exc
        return PublishReceipt(thread_id=result.thread.id, message_id=result.message.id)

    async def update_story(
        self,
        story: Story,
        articles: list[Article],
        article: Article,
        related_stories: list[Story],
        updates: list[StoryUpdate],
        *,
        post_update: bool,
    ) -> int | None:
        if story.discord_thread_id is None or story.discord_starter_message_id is None:
            raise PublishError("story has no confirmed Discord thread", uncertain=False)
        thread = await self._thread(story.discord_thread_id)
        try:
            if thread.archived:
                await thread.edit(archived=False, reason=f"Big story {story.id} update")
            expected_name = forum_title(story.title)
            if thread.name != expected_name:
                await thread.edit(name=expected_name, reason=f"Big story {story.id} title")
            parent = thread.parent
            if isinstance(parent, discord.ForumChannel):
                tags = self._resolve_tags(parent, story.tags, ())
                if tags:
                    await thread.edit(applied_tags=tags, reason=f"Big story {story.id} tags")
            starter = await thread.fetch_message(story.discord_starter_message_id)
            content = neutralize_mentions(_starter_content(story))
            embed = _story_embed(
                story,
                articles,
                related_stories,
                updates,
                public_site_url=self._public_site_url,
            )
            if not any(item.filename == BRAND_ICON_FILENAME for item in starter.attachments):
                icon = _brand_icon_file()
                try:
                    await starter.edit(
                        content=content,
                        embed=embed,
                        attachments=[*starter.attachments, icon],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                finally:
                    icon.close()
            else:
                await starter.edit(
                    content=content,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            update_detail = next(
                (
                    update.detail
                    for update in reversed(updates)
                    if update.article.id == article.id and update.detail
                ),
                None,
            )
            if not post_update or not (
                update_detail or story_update_detail(article.title, article.description, limit=2600)
            ):
                return None
            icon = _brand_icon_file()
            try:
                message = await thread.send(
                    embed=_update_embed(article, detail=update_detail),
                    file=icon,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                icon.close()
            return message.id
        except discord.HTTPException as exc:
            raise _publish_error("update forum story", exc) from exc

    async def mark_merged(self, source: Story, target: Story) -> None:
        if source.discord_thread_id is None or target.discord_thread_id is None:
            return
        thread = await self._thread(source.discord_thread_id)
        url = f"https://discord.com/channels/{source.guild_id}/{target.discord_thread_id}"
        try:
            await thread.send(
                f"This story was merged into [the main thread]({url}).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await thread.edit(
                name=forum_title(f"Merged: {source.title}"),
                archived=True,
                reason=f"Merged into Big story {target.id}",
            )
        except discord.HTTPException as exc:
            raise _publish_error("mark merged story", exc) from exc

    async def archive_story(self, story: Story) -> None:
        if story.discord_thread_id is None:
            return
        try:
            thread = await self._thread(story.discord_thread_id)
            if not thread.archived:
                await thread.edit(archived=True, reason=f"Big retention story {story.id}")
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            raise _publish_error("archive old forum story", exc) from exc

    async def delete_story(self, story: Story) -> None:
        if story.discord_thread_id is None:
            return
        try:
            thread = await self._thread(story.discord_thread_id)
            await thread.delete(reason=f"Big retention story {story.id}")
        except discord.NotFound:
            return
        except discord.HTTPException as exc:
            raise _publish_error("delete old forum story", exc) from exc

    async def _forum(self, channel_id: int, guild_id: int) -> discord.ForumChannel:
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                raise _publish_error("fetch forum channel", exc) from exc
        if not isinstance(channel, discord.ForumChannel):
            raise PublishError("configured destination is not a forum channel", uncertain=False)
        if channel.guild.id != guild_id:
            raise PublishError("configured forum belongs to another guild", uncertain=False)
        return channel

    async def _thread(self, thread_id: int) -> discord.Thread:
        channel = self._client.get_channel(thread_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(thread_id)
            except discord.HTTPException as exc:
                raise _publish_error("fetch forum thread", exc) from exc
        if not isinstance(channel, discord.Thread):
            raise PublishError("Discord destination is not a forum thread", uncertain=False)
        return channel

    @staticmethod
    def _resolve_tags(
        forum: discord.ForumChannel, names: tuple[str, ...], tag_ids: tuple[int, ...]
    ) -> list[discord.ForumTag]:
        by_id = {tag.id: tag for tag in forum.available_tags}
        by_name = {tag.name.casefold(): tag for tag in forum.available_tags}
        resolved: list[discord.ForumTag] = []
        for tag_id in tag_ids:
            if tag := by_id.get(tag_id):
                resolved.append(tag)
        for name in names:
            if (tag := by_name.get(name.casefold())) and tag not in resolved:
                resolved.append(tag)
        if forum.flags.require_tag and not resolved:
            raise PublishError(
                "forum requires a tag but no configured tag exists there", uncertain=False
            )
        return resolved[:5]


class DryRunForumPublisher:
    def __init__(self) -> None:
        self._next_id = 10_000

    async def create_story(
        self,
        feed: Feed,
        story: Story,
        articles: list[Article],
        related_stories: list[Story],
        updates: list[StoryUpdate],
    ) -> PublishReceipt:
        self._next_id += 1
        log.info(
            "dry-run story created",
            extra={"event": "dry_run_create", "story_id": story.id, "feed_id": feed.id},
        )
        return PublishReceipt(thread_id=self._next_id, message_id=self._next_id)

    async def update_story(
        self,
        story: Story,
        articles: list[Article],
        article: Article,
        related_stories: list[Story],
        updates: list[StoryUpdate],
        *,
        post_update: bool,
    ) -> int | None:
        self._next_id += 1
        log.info(
            "dry-run story updated",
            extra={"event": "dry_run_update", "story_id": story.id, "article_id": article.id},
        )
        return self._next_id if post_update else None

    async def mark_merged(self, source: Story, target: Story) -> None:
        log.info(
            "dry-run stories merged",
            extra={"event": "dry_run_merge", "story_id": target.id},
        )

    async def archive_story(self, story: Story) -> None:
        log.info(
            "dry-run story archived",
            extra={"event": "dry_run_archive", "story_id": story.id},
        )

    async def delete_story(self, story: Story) -> None:
        log.info(
            "dry-run story deleted",
            extra={"event": "dry_run_delete", "story_id": story.id},
        )


def _starter_content(story: Story) -> str:
    return f"**{story.state.value.upper()}**  |  Story `{story.id}`"


def _story_embed(
    story: Story,
    articles: list[Article],
    related_stories: list[Story],
    updates: list[StoryUpdate],
    *,
    public_site_url: str = "https://bigif.org",
) -> discord.Embed:
    primary = next(
        (article for article in articles if article.id == story.primary_article_id),
        articles[0] if articles else None,
    )
    embed = discord.Embed(
        title=forum_title(story.title),
        description=_story_description(story),
        url=_web_story_url(public_site_url, story.id),
        color=_state_color(story),
        timestamp=(
            (primary.published_at or primary.discovered_at)
            if primary
            else (story.first_published_at or story.last_updated_at)
        ),
    )
    if primary:
        embed.add_field(
            name="Primary source",
            value=(
                f"[{publisher_label(primary.publisher, primary.url)}]"
                f"({safe_external_link(primary.url)})"
            ),
            inline=False,
        )
    primary_url = primary.canonical_url if primary is not None else ""
    sources: list[str] = []
    seen_urls = {primary_url} if primary_url else set()
    for article in articles:
        if article.canonical_url in seen_urls:
            continue
        url = safe_external_link(article.url)
        if url:
            seen_urls.add(article.canonical_url)
            sources.append(f"- [{publisher_label(article.publisher, article.url)}]({url})")
    shown_sources = sources[:11]
    if len(sources) > 11:
        shown_sources.append(f"- {len(sources) - 11} more sources")
    if shown_sources:
        embed.add_field(
            name=f"More sources ({len(sources)})",
            value="\n".join(shown_sources),
            inline=False,
        )
    if story.analysis_state is AnalysisState.READY and story.analysis:
        analysis_sources = analysis_display(story.analysis).sources
        article_urls = {article.canonical_url for article in articles}
        unique_analysis_sources = [
            (label, url)
            for label, url in analysis_sources
            if url not in article_urls and url not in {article.url for article in articles}
        ]
        if unique_analysis_sources:
            value = "\n".join(
                f"- [{publisher_label(label, url)}]({url})"
                for label, url in unique_analysis_sources
            )
            embed.add_field(name="Additional sources", value=value[:1024], inline=False)
    visible_updates = visible_story_updates(primary, updates)
    if visible_updates:
        update_lines = []
        shown_updates = visible_updates[-4:]
        if len(visible_updates) > len(shown_updates):
            earlier_count = len(visible_updates) - len(shown_updates)
            update_lines.append(f"{earlier_count} earlier updates on the story page")
        for update in shown_updates:
            article = update.article
            link = safe_external_link(article.url)
            detail = update.detail or story_update_detail(
                article.title, article.description, limit=170
            )
            if not detail:
                detail = "The source did not provide separate update details."
            source = (
                f"[{publisher_label(article.publisher, article.url)}: "
                f"{plain_text(article.title, limit=100)}]({link})"
                if link
                else (
                    f"{publisher_label(article.publisher, article.url)}: "
                    f"{plain_text(article.title, limit=100)}"
                )
            )
            timestamp = int((article.published_at or update.recorded_at).timestamp())
            update_lines.append(f"**{detail}**\n{source}  <t:{timestamp}:R>")
        embed.add_field(name="Updates", value="\n".join(update_lines)[:1024], inline=False)
    related = []
    for candidate in related_stories:
        if candidate.discord_thread_id is None:
            continue
        url = f"https://discord.com/channels/{candidate.guild_id}/{candidate.discord_thread_id}"
        related.append(f"- [{plain_text(candidate.title, limit=100)}]({url})")
    if related:
        embed.add_field(
            name="Related stories",
            value="\n".join(related)[:1024],
            inline=False,
        )
    embed.add_field(
        name="Big If True",
        value=f"[Open story page]({_web_story_url(public_site_url, story.id)})",
        inline=False,
    )
    embed.set_footer(text="Published", icon_url=BRAND_ICON_URI)
    return embed


def _story_description(story: Story) -> str:
    if story.analysis_state is AnalysisState.READY and story.analysis:
        return neutralize_mentions(analysis_display(story.analysis).body)[:3000].rstrip()
    return plain_text(story.summary, limit=3000)


def _update_embed(article: Article, *, detail: str | None = None) -> discord.Embed:
    link = safe_external_link(article.url)
    update_detail = detail or story_update_detail(article.title, article.description, limit=2600)
    if not update_detail:
        update_detail = "The source did not provide separate update details."
    source = (
        f"[{publisher_label(article.publisher, article.url)}: "
        f"{plain_text(article.title, limit=240)}]({link})"
        if link
        else (
            f"{publisher_label(article.publisher, article.url)}: "
            f"{plain_text(article.title, limit=240)}"
        )
    )
    embed = discord.Embed(
        title="Story update",
        description=f"{update_detail}\n\n**Source**\n{source}",
        url=link or None,
        color=discord.Color.orange(),
        timestamp=article.published_at,
    )
    embed.set_footer(text="Published", icon_url=BRAND_ICON_URI)
    return embed


def _state_color(story: Story) -> discord.Color:
    if story.state.value == "breaking":
        return discord.Color.red()
    if story.state.value == "developing":
        return discord.Color.orange()
    return discord.Color.from_rgb(88, 101, 242)


def _web_story_url(public_site_url: str, story_id: int) -> str:
    return f"{public_site_url.rstrip('/')}/news/story/{story_id}/"


def _brand_icon_file() -> discord.File:
    return discord.File(BRAND_ICON_PATH, filename=BRAND_ICON_FILENAME)


def _publish_error(action: str, error: discord.HTTPException) -> PublishError:
    confirmed = isinstance(error, (discord.Forbidden, discord.NotFound)) or (
        400 <= error.status < 500 and error.status != 429
    )
    return PublishError(
        f"Discord could not {action}: HTTP {error.status}, code {error.code}",
        uncertain=not confirmed,
    )
