from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from bigbot.domain import AnalysisState, Article, DeliveryState, PublicationState, Story, StoryState
from bigbot.publisher import BRAND_ICON_URI, _story_embed


def _story(story_id: int, title: str, thread_id: int) -> Story:
    now = datetime.now(UTC)
    return Story(
        id=story_id,
        guild_id=1,
        forum_channel_id=2,
        title=title,
        summary="Deterministic summary",
        state=StoryState.NEW,
        publication_state=PublicationState.PUBLISHED,
        discord_thread_id=thread_id,
        discord_starter_message_id=thread_id + 1,
        tags=(),
        normalized_title=title.casefold(),
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        first_published_at=now,
        last_published_at=now,
        last_updated_at=now,
        analysis="**Summary**\nCurrent story analysis.\n\n**Key facts**\n- One fact.",
        analysis_state=AnalysisState.READY,
    )


def test_story_embed_renders_analysis_and_related_thread_link() -> None:
    current = _story(1, "Current story", 101)
    related = _story(2, "Directly related story", 202)
    embed = _story_embed(current, [], [related]).to_dict()
    assert embed["description"] == current.analysis
    related_field = next(field for field in embed["fields"] if field["name"] == "Related stories")
    assert "https://discord.com/channels/1/202" in related_field["value"]
    assert "Directly related story" in related_field["value"]
    assert embed["footer"] == {"text": "Published", "icon_url": BRAND_ICON_URI}
    assert embed["timestamp"] == current.first_published_at.isoformat()  # type: ignore[union-attr]
    assert embed["url"] == "https://bigif.org/news/#story-1"


def test_story_embed_separates_analysis_sources_from_body() -> None:
    story = _story(1, "Current story", 101)
    story = replace(
        story,
        analysis=(
            "**Summary**\nCurrent story analysis.\n\n**Key facts**\n- One fact.\n\n"
            "**Analysis sources**\n- [Public record](https://example.com/record)"
        ),
    )
    embed = _story_embed(story, [], []).to_dict()
    assert "Analysis sources" not in embed["description"]
    sources = next(field for field in embed["fields"] if field["name"] == "Analysis sources")
    assert sources["value"] == "- [Public record](https://example.com/record)"


def test_story_embed_puts_article_time_only_in_footer_timestamp() -> None:
    story = _story(1, "Clean title", 101)
    published = datetime(2026, 9, 2, 15, 30, tzinfo=UTC)
    article = Article(
        id=8,
        feed_id=1,
        story_id=story.id,
        external_id="source-8",
        publisher="Wire",
        title="Clean title",
        url="https://example.com/story",
        canonical_url="https://example.com/story",
        published_at=published,
        description="Story description",
        discovered_at=published,
        normalized_title="clean title",
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        fingerprint="fingerprint",
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
    )
    embed = _story_embed(story, [article], []).to_dict()
    primary = next(field for field in embed["fields"] if field["name"] == "Primary source")
    assert "<t:" not in primary["value"]
    assert embed["timestamp"] == published.isoformat()
