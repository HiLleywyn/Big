from __future__ import annotations

import logging
from typing import Protocol

import discord

from bigbot.domain import AnalysisState, Article, Feed, PublishReceipt, Story
from bigbot.security import forum_title, neutralize_mentions, plain_text, safe_external_link

log = logging.getLogger(__name__)


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
    ) -> PublishReceipt: ...

    async def update_story(
        self,
        story: Story,
        articles: list[Article],
        article: Article,
        related_stories: list[Story],
        *,
        post_update: bool,
    ) -> int | None: ...

    async def mark_merged(self, source: Story, target: Story) -> None: ...

    async def archive_story(self, story: Story) -> None: ...

    async def delete_story(self, story: Story) -> None: ...


class DiscordForumPublisher:
    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def create_story(
        self,
        feed: Feed,
        story: Story,
        articles: list[Article],
        related_stories: list[Story],
    ) -> PublishReceipt:
        channel = await self._forum(feed.forum_channel_id, feed.guild_id)
        tags = self._resolve_tags(channel, story.tags, feed.tag_ids)
        try:
            result = await channel.create_thread(
                name=forum_title(story.title),
                content=neutralize_mentions(_starter_content(story)),
                embed=_story_embed(story, articles, related_stories),
                applied_tags=tags,
                auto_archive_duration=1440,
                allowed_mentions=discord.AllowedMentions.none(),
                reason=f"Big story {story.id}",
            )
        except discord.HTTPException as exc:
            raise _publish_error("create forum story", exc) from exc
        return PublishReceipt(thread_id=result.thread.id, message_id=result.message.id)

    async def update_story(
        self,
        story: Story,
        articles: list[Article],
        article: Article,
        related_stories: list[Story],
        *,
        post_update: bool,
    ) -> int | None:
        if story.discord_thread_id is None or story.discord_starter_message_id is None:
            raise PublishError("story has no confirmed Discord thread", uncertain=False)
        thread = await self._thread(story.discord_thread_id)
        try:
            if thread.archived:
                await thread.edit(archived=False, reason=f"Big story {story.id} update")
            parent = thread.parent
            if isinstance(parent, discord.ForumChannel):
                tags = self._resolve_tags(parent, story.tags, ())
                if tags:
                    await thread.edit(applied_tags=tags, reason=f"Big story {story.id} tags")
            starter = await thread.fetch_message(story.discord_starter_message_id)
            await starter.edit(
                content=neutralize_mentions(_starter_content(story)),
                embed=_story_embed(story, articles, related_stories),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if not post_update:
                return None
            message = await thread.send(
                embed=_update_embed(article),
                allowed_mentions=discord.AllowedMentions.none(),
            )
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
    story: Story, articles: list[Article], related_stories: list[Story]
) -> discord.Embed:
    primary = next(
        (article for article in articles if article.id == story.primary_article_id),
        articles[0] if articles else None,
    )
    embed = discord.Embed(
        title=plain_text(story.title, limit=256),
        description=_story_description(story),
        color=_state_color(story),
        timestamp=story.last_updated_at,
    )
    if primary:
        published = _discord_time(primary.published_at)
        embed.add_field(
            name="Primary source",
            value=(
                f"[{plain_text(primary.publisher, limit=100)}]"
                f"({safe_external_link(primary.url)})\n{published}"
            ),
            inline=False,
        )
    sources = []
    for article in articles[:12]:
        url = safe_external_link(article.url)
        if url:
            sources.append(f"- [{plain_text(article.publisher, limit=80)}]({url})")
    if len(articles) > 12:
        sources.append(f"- {len(articles) - 12} more sources")
    embed.add_field(
        name=f"Sources ({len(articles)})", value="\n".join(sources) or "None", inline=False
    )
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
    embed.set_footer(text=f"Last updated | {_discord_time(story.last_updated_at)}")
    return embed


def _story_description(story: Story) -> str:
    if story.analysis_state is AnalysisState.READY and story.analysis:
        return neutralize_mentions(story.analysis)[:3000].rstrip()
    return plain_text(story.summary, limit=3000)


def _update_embed(article: Article) -> discord.Embed:
    link = safe_external_link(article.url)
    embed = discord.Embed(
        title=f"Update from {plain_text(article.publisher, limit=180)}",
        description=plain_text(article.description, limit=3000),
        url=link or None,
        color=discord.Color.orange(),
        timestamp=article.published_at,
    )
    embed.set_footer(text="Source added to this story")
    return embed


def _state_color(story: Story) -> discord.Color:
    if story.state.value == "breaking":
        return discord.Color.red()
    if story.state.value == "developing":
        return discord.Color.orange()
    return discord.Color.from_rgb(88, 101, 242)


def _discord_time(value: object) -> str:
    timestamp = int(value.timestamp()) if hasattr(value, "timestamp") else 0
    return f"<t:{timestamp}:F>" if timestamp else "Publication time unavailable"


def _publish_error(action: str, error: discord.HTTPException) -> PublishError:
    confirmed = isinstance(error, (discord.Forbidden, discord.NotFound)) or (
        400 <= error.status < 500 and error.status != 429
    )
    return PublishError(
        f"Discord could not {action}: HTTP {error.status}, code {error.code}",
        uncertain=not confirmed,
    )
