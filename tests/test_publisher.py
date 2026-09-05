from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from bigbot.domain import (
    AnalysisState,
    Article,
    DeliveryState,
    PublicationState,
    Story,
    StoryState,
    StoryUpdate,
    WeeklySummary,
)
from bigbot.publisher import (
    BRAND_ICON_URI,
    _embed_character_count,
    _story_embed,
    _weekly_summary_embeds,
)


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
    embed = _story_embed(current, [], [related], []).to_dict()
    assert embed["description"] == (
        "**Summary**\n\nCurrent story analysis.\n\n**Key facts**\n\n- One fact."
    )
    related_field = next(field for field in embed["fields"] if field["name"] == "Related stories")
    assert "https://discord.com/channels/1/202" in related_field["value"]
    assert "Directly related story" in related_field["value"]
    assert embed["footer"] == {"text": "Published", "icon_url": BRAND_ICON_URI}
    assert embed["timestamp"] == current.first_published_at.isoformat()  # type: ignore[union-attr]
    assert embed["url"] == "https://bigif.org/news/story/1/"


def test_weekly_summary_uses_full_cards_and_only_big_story_links() -> None:
    now = datetime.now(UTC)
    story = replace(
        _story(1, "Central bank changes its policy", 101),
        tags=("Markets", "Economy"),
        analysis=(
            "**Summary**\nThe central bank changed its benchmark rate after its policy meeting. "
            "Officials said the new rate takes effect on Monday.\n\n"
            "**Key facts**\n- The benchmark rate changed by 25 basis points.\n"
            "- The vote was unanimous.\n\n"
            "**Analysis sources**\n- [Wire](https://publisher.example/article)"
        ),
    )
    weekly = WeeklySummary(
        id=1,
        guild_id=1,
        forum_channel_id=2,
        week_start=now,
        week_end=now,
        title="Weekly Summary",
        overview="The week's largest stories.",
        story_ids=(story.id,),
        discord_thread_id=500,
        discord_starter_message_id=501,
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
        generated_at=now,
        updated_at=now,
    )

    embeds = _weekly_summary_embeds(
        weekly,
        [story],
        source_counts={story.id: 3},
        public_site_url="https://bigif.org",
    )
    card = embeds[1].to_dict()
    rendered = str(card)

    assert card["url"] == "https://bigif.org/news/story/1/"
    assert "Officials said the new rate takes effect on Monday." in card["description"]
    assert "..." not in rendered
    assert "Key facts" in {field["name"] for field in card["fields"]}
    assert "3 sources" in rendered
    assert "https://bigif.org/news/story/1/" in rendered
    assert "publisher.example" not in rendered


def test_weekly_summary_keeps_all_eight_stories_within_discord_budget() -> None:
    now = datetime.now(UTC)
    base = _story(1, "A significant international development with a detailed headline", 101)
    long_fact = (
        "Officials published a detailed verified finding with dates, figures, and context. " * 4
    )
    stories = [
        replace(
            base,
            id=index,
            title=f"{base.title} {index}",
            analysis=(
                f"**Summary**\n{long_fact}\n\n**Key facts**\n- {long_fact}\n- {long_fact}\n\n"
                f"**Unclear or disputed**\n- {long_fact}"
            ),
        )
        for index in range(1, 9)
    ]
    weekly = WeeklySummary(
        id=1,
        guild_id=1,
        forum_channel_id=2,
        week_start=now,
        week_end=now,
        title="Weekly Summary",
        overview="The week's largest stories.",
        story_ids=tuple(story.id for story in stories),
        discord_thread_id=500,
        discord_starter_message_id=501,
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
        generated_at=now,
        updated_at=now,
    )

    embeds = _weekly_summary_embeds(
        weekly,
        stories,
        source_counts={story.id: 3 for story in stories},
        public_site_url="https://bigif.org",
    )

    assert len(embeds) == 9
    assert sum(_embed_character_count(embed) for embed in embeds) <= 5900
    assert all("..." not in str(embed.to_dict()) for embed in embeds)


def test_story_embed_separates_analysis_sources_from_body() -> None:
    story = _story(1, "Current story", 101)
    story = replace(
        story,
        analysis=(
            "**Summary**\nCurrent story analysis.\n\n**Key facts**\n- One fact.\n\n"
            "**Analysis sources**\n- [Public record](https://example.com/record)"
        ),
    )
    embed = _story_embed(story, [], [], []).to_dict()
    assert "Analysis sources" not in embed["description"]
    sources = next(field for field in embed["fields"] if field["name"] == "Additional sources")
    assert sources["value"] == "- [Public record](https://example.com/record)"


def test_story_embed_does_not_repeat_headline_as_summary_and_fact() -> None:
    title = "Trump will allocate $400 million to $500 million from super PAC - Reuters"
    story = replace(
        _story(1, title, 101),
        analysis=(
            "**Summary**\nTrump will allocate $400 million to $500 million from super PAC.\n\n"
            "**Key facts**\n"
            "- Trump will allocate $400 million to $500 million from super PAC."
        ),
    )

    embed = _story_embed(story, [], [], []).to_dict()

    assert embed["description"] == "The source supplied no verified details beyond the headline."


def test_story_embed_removes_cross_section_rephrasing() -> None:
    story = replace(
        _story(1, "Trump signs orders concerning ranchers and meat processing", 101),
        analysis=(
            "**Summary**\n"
            "Trump signed orders concerning ranchers, wolves, and meat processing. "
            "The directives aim to lower record beef prices.\n\n"
            "**Key facts**\n"
            "- Trump signed the ranching and meat-processing orders.\n"
            "- The orders aim to bring down record beef prices.\n"
            "- The signing followed a meeting with a small group of ranchers."
        ),
    )

    embed = _story_embed(story, [], [], []).to_dict()

    assert "bring down record beef prices" not in embed["description"]
    assert "signed the ranching and meat-processing orders" not in embed["description"]
    assert "meeting with a small group of ranchers" in embed["description"]


def test_story_embed_keeps_summary_that_adds_context_to_headline() -> None:
    story = replace(
        _story(1, "Greer says agriculture announcements likely during Xi visit", 101),
        analysis=(
            "**Summary**\n"
            "Greer said agriculture announcements are likely during Xi's visit. "
            "He made the remarks in a Fox News interview and said the countries are "
            "managing their relationship rather than seeking a comprehensive agreement.\n\n"
            "**Key facts**\n"
            "- Greer said agriculture announcements are likely during Xi's visit."
        ),
    )

    embed = _story_embed(story, [], [], []).to_dict()

    assert "**Summary**" in embed["description"]
    assert "Fox News interview" in embed["description"]
    assert "**Key facts**" not in embed["description"]


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
    embed = _story_embed(story, [article], [], []).to_dict()
    primary = next(field for field in embed["fields"] if field["name"] == "Primary source")
    assert "<t:" not in primary["value"]
    assert embed["timestamp"] == published.isoformat()


def test_story_embed_separates_updates_from_original_report() -> None:
    story = _story(1, "Clean title", 101)
    published = datetime(2026, 9, 2, 15, 30, tzinfo=UTC)
    article = Article(
        id=8,
        feed_id=1,
        story_id=story.id,
        external_id="source-8",
        publisher="Wire",
        title="Officials publish a new count",
        url="https://example.com/update",
        canonical_url="https://example.com/update",
        published_at=published,
        description="New confirmed figures were published.",
        discovered_at=published,
        normalized_title="official publish new count",
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        fingerprint="fingerprint-update",
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
    )
    update = StoryUpdate(article=article, kind="major_update", recorded_at=published)
    original = replace(
        article,
        id=7,
        title="Officials begin counting votes",
        url="https://example.com/original",
        canonical_url="https://example.com/original",
        description="The count began after polls closed.",
    )

    embed = _story_embed(story, [original, article], [], [update]).to_dict()

    updates = next(field for field in embed["fields"] if field["name"] == "Updates")
    assert "New confirmed figures were published." in updates["value"]
    assert "Wire: Officials publish a new count" in updates["value"]
    assert "<t:1788363000:R>" in updates["value"]


def test_story_embed_hides_repeated_transport_copy_from_updates() -> None:
    story = _story(1, "Clean title", 101)
    published = datetime(2026, 9, 2, 15, 30, tzinfo=UTC)
    original = Article(
        id=8,
        feed_id=1,
        story_id=story.id,
        external_id="rss-8",
        publisher="Reuters",
        title="Officials publish a new count - Reuters",
        url="https://news.google.com/rss/articles/ABC?oc=5",
        canonical_url="https://news.google.com/rss/articles/ABC?oc=5",
        published_at=published,
        description="Officials publish a new count Reuters",
        discovered_at=published,
        normalized_title="official publish new count",
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        fingerprint="fingerprint-update",
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
    )
    duplicate = replace(
        original,
        id=9,
        external_id="atom-8",
        url="https://news.google.com/atom/articles/ABC?oc=5",
        canonical_url="https://news.google.com/atom/articles/ABC?oc=5",
    )
    update = StoryUpdate(article=duplicate, kind="major_update", recorded_at=published)

    embed = _story_embed(story, [original, duplicate], [], [update]).to_dict()

    assert all(field["name"] != "Updates" for field in embed["fields"])


def test_story_embed_cleans_google_news_source_and_avoids_redundant_fields() -> None:
    story = replace(
        _story(1, "Pacific update - reuters.com", 101),
        analysis=(
            "**Summary**\nCurrent update.\n\n**Key facts**\n- One fact.\n\n"
            "**Analysis sources**\n"
            '- ["site:reuters.com" - Google News](https://news.google.com/story)\n'
            "- [Public record](https://example.gov/record)"
        ),
    )
    published = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    article = Article(
        id=9,
        feed_id=1,
        story_id=story.id,
        external_id="source-9",
        publisher='"site:reuters.com" - Google News',
        title=story.title,
        url="https://news.google.com/story",
        canonical_url="https://news.google.com/story",
        published_at=published,
        description="Story description",
        discovered_at=published,
        normalized_title="pacific update",
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        fingerprint="fingerprint-9",
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
    )
    embed = _story_embed(story, [article], [], []).to_dict()
    assert embed["title"] == "Pacific update"
    primary = next(field for field in embed["fields"] if field["name"] == "Primary source")
    assert primary["value"] == "[Reuters](https://news.google.com/story)"
    assert not any(field["name"].startswith("More sources") for field in embed["fields"])
    analysis = next(field for field in embed["fields"] if field["name"] == "Additional sources")
    assert "Google News" not in analysis["value"]
    assert "Public record" in analysis["value"]
