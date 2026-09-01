from __future__ import annotations

from datetime import UTC, datetime

from bigbot.domain import AnalysisState, PublicationState, Story, StoryState
from bigbot.publisher import _story_embed


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
    assert embed["footer"]["text"] == "Last updated"
    assert "<t:" not in embed["footer"]["text"]
    assert embed["timestamp"] == current.last_updated_at.isoformat()
