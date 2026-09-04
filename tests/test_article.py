from __future__ import annotations

import ipaddress

import discord
import httpx2
import pytest

from bigbot.article import ArticleExtractionError, ArticleExtractor, extract_article_urls
from bigbot.bot import ArticleResultView
from bigbot.domain import (
    AnalysisState,
    Article,
    DeliveryState,
    PublicationState,
    Story,
    StoryState,
    utc_now,
)
from bigbot.service import ProcessedItem


async def _public_resolver(
    hostname: str, port: int
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    assert hostname
    assert port == 443
    return {ipaddress.ip_address("93.184.216.34")}


def test_extract_article_urls_from_message_and_embed_text() -> None:
    urls = extract_article_urls(
        (
            "Read <https://news.example/story?utm_source=discord>.",
            "Source: https://wire.example/report), then discuss it.",
            "https://news.example/story?utm_source=discord",
            "Ignore http://insecure.example/story",
        )
    )
    assert urls == (
        "https://news.example/story?utm_source=discord",
        "https://wire.example/report",
    )


async def test_article_extractor_reads_open_graph_and_canonical_metadata() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://news.example/story"
        return httpx2.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="""
                <html><head>
                <title>Fallback title</title>
                <meta property="og:title" content="Officials approve a new policy">
                <meta property="og:description" content="Officials approved the policy Friday.">
                <meta property="og:site_name" content="Example News">
                <meta property="article:published_time" content="2026-09-04T12:30:00Z">
                <link rel="canonical" href="https://news.example/story">
                </head><body></body></html>
            """,
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    extractor = ArticleExtractor(
        timeout_seconds=10,
        max_bytes=100_000,
        resolver=_public_resolver,
        client=client,
    )
    item = await extractor.fetch("https://news.example/story")
    assert item.title == "Officials approve a new policy"
    assert item.summary == "Officials approved the policy Friday."
    assert item.publisher == "Example News"
    assert item.url == "https://news.example/story"
    assert item.published_at is not None
    assert item.external_id.startswith("manual:")
    await client.aclose()


async def test_article_extractor_rejects_non_html_content() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"not an article",
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    extractor = ArticleExtractor(
        timeout_seconds=10,
        max_bytes=100_000,
        resolver=_public_resolver,
        client=client,
    )
    with pytest.raises(ArticleExtractionError, match="not an HTML article"):
        await extractor.fetch("https://news.example/file.pdf")
    await client.aclose()


def test_article_result_is_components_v2_with_story_links() -> None:
    now = utc_now()
    story = Story(
        id=7,
        guild_id=1,
        forum_channel_id=2,
        title="Officials approve a new policy",
        summary="Officials approved the policy Friday.",
        state=StoryState.NEW,
        publication_state=PublicationState.PUBLISHED,
        discord_thread_id=70,
        discord_starter_message_id=71,
        tags=("Politics",),
        normalized_title="officials approve policy",
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        first_published_at=now,
        last_published_at=now,
        last_updated_at=now,
        analysis=(
            "**Summary**\nOfficials approved the policy.\n\n"
            "**Key facts**\n- The vote occurred Friday.\n\n"
            "**Useful context**\n- The policy takes effect next month.\n\n"
            "**Analysis sources**\n- [Example News](https://news.example/story)"
        ),
        analysis_state=AnalysisState.READY,
    )
    article = Article(
        id=8,
        feed_id=1,
        story_id=7,
        external_id="manual:one",
        publisher="Example News",
        title=story.title,
        url="https://news.example/story",
        canonical_url="https://news.example/story",
        published_at=now,
        description=story.summary,
        discovered_at=now,
        normalized_title=story.normalized_title,
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        fingerprint="one",
        delivery_state=DeliveryState.POSTED,
        delivery_error=None,
    )
    view = ArticleResultView(
        ProcessedItem("new_stories", article, story, ()),
        public_site_url="https://bigif.org",
    )
    assert isinstance(view, discord.ui.LayoutView)
    assert len(view.children) == 1
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    rendered = str(container.to_component_dict())
    assert "Useful context" in rendered
    assert "https://bigif.org/news/story/7/" in rendered
    assert "https://discord.com/channels/1/70" in rendered
