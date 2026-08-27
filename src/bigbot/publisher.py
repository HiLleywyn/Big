from __future__ import annotations

from typing import Protocol

import discord

from bigbot.domain import Feed, FeedItem, PublishReceipt
from bigbot.security import forum_title, neutralize_mentions, plain_text, safe_external_link


class PublishError(RuntimeError):
    pass


class ForumPublisher(Protocol):
    async def publish(self, feed: Feed, item: FeedItem) -> PublishReceipt: ...

    async def reply(self, thread_id: int, content: str) -> int: ...


class DiscordForumPublisher:
    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def publish(self, feed: Feed, item: FeedItem) -> PublishReceipt:
        channel = self._client.get_channel(feed.forum_channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(feed.forum_channel_id)
            except discord.HTTPException as exc:
                raise PublishError("Discord forum channel could not be fetched") from exc
        if not isinstance(channel, discord.ForumChannel):
            raise PublishError("configured destination is not a Discord forum channel")
        if channel.guild.id != feed.guild_id:
            raise PublishError("configured forum channel belongs to a different guild")

        tags_by_id = {tag.id: tag for tag in channel.available_tags}
        missing_tags = set(feed.tag_ids) - tags_by_id.keys()
        if missing_tags:
            raise PublishError(f"configured forum tags no longer exist: {sorted(missing_tags)}")
        applied_tags = [tags_by_id[tag_id] for tag_id in feed.tag_ids]
        if channel.flags.require_tag and not applied_tags:
            raise PublishError("the forum requires a tag, but this feed has none configured")

        embed = discord.Embed(
            title=plain_text(item.title, limit=256),
            description=plain_text(item.summary, limit=4000),
            url=safe_external_link(item.url) or None,
            color=discord.Color.from_rgb(88, 101, 242),
            timestamp=item.published_at,
        )
        if item.author:
            embed.set_author(name=plain_text(item.author, limit=256))
        image_url = safe_external_link(item.image_url or "")
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text=f"Big · {plain_text(feed.name, limit=80)}")

        link = safe_external_link(item.url)
        starter = f"[Open source]({link})" if link else "Source link unavailable"
        try:
            result = await channel.create_thread(
                name=forum_title(item.title),
                content=neutralize_mentions(starter),
                embed=embed,
                applied_tags=applied_tags,
                auto_archive_duration=1440,
                allowed_mentions=discord.AllowedMentions.none(),
                reason=f"Big feed {feed.id}: {item.external_id[:80]}",
            )
        except discord.HTTPException as exc:
            raise PublishError(
                f"Discord rejected or did not confirm the forum post: {exc.code}"
            ) from exc
        return PublishReceipt(thread_id=result.thread.id, message_id=result.message.id)

    async def reply(self, thread_id: int, content: str) -> int:
        channel = self._client.get_channel(thread_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(thread_id)
            except discord.HTTPException as exc:
                raise PublishError("Discord forum thread could not be fetched") from exc
        if not isinstance(channel, discord.Thread):
            raise PublishError("Discord destination is no longer a forum thread")
        try:
            message = await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=False,
            )
        except discord.HTTPException as exc:
            raise PublishError(f"Discord did not confirm the AI reply: {exc.code}") from exc
        return message.id
