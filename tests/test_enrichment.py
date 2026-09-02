from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx2
import pytest

from bigbot.domain import (
    Article,
    DeliveryState,
    PublicationState,
    Story,
    StoryState,
)
from bigbot.enrichment import EnrichmentError, OpenRouterEnricher


def story(story_id: int, title: str = "A policy changed") -> Story:
    now = datetime.now(UTC)
    return Story(
        id=story_id,
        guild_id=1,
        forum_channel_id=2,
        title=title,
        summary="Available reports describe a policy change.",
        state=StoryState.NEW,
        publication_state=PublicationState.PUBLISHED,
        discord_thread_id=100 + story_id,
        discord_starter_message_id=200 + story_id,
        tags=("Politics",),
        normalized_title=title.casefold(),
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        first_published_at=now,
        last_published_at=now,
        last_updated_at=now,
    )


def article(article_id: int, publisher: str) -> Article:
    now = datetime.now(UTC)
    return Article(
        id=article_id,
        feed_id=1,
        story_id=1,
        external_id=str(article_id),
        publisher=publisher,
        title=f"{publisher} reports the policy change",
        url=f"https://{publisher.casefold()}.example/story",
        canonical_url=f"https://{publisher.casefold()}.example/story",
        published_at=now,
        description=f"{publisher} describes the available facts.",
        discovered_at=now,
        normalized_title="policy change",
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        fingerprint=f"fingerprint-{article_id}",
        delivery_state=DeliveryState.PENDING,
        delivery_error=None,
    )


async def test_story_analysis_uses_all_sources_and_validates_structure() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body["provider"] == {
            "data_collection": "deny",
            "zdr": True,
        }
        assert body["model"] == "deepseek/deepseek-v4-flash-0731"
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        supplied = json.loads(body["messages"][1]["content"])
        assert [item["publisher"] for item in supplied["articles"]] == ["Reuters", "AP"]
        assert set(supplied["articles"][0]) == {
            "title",
            "description",
            "publisher",
            "url",
            "published_at",
        }
        assert supplied["relationship_candidates"][0]["story_id"] == 9
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Officials announced a policy change.",
                                    "key_facts": ["Reuters and AP describe the same announcement."],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [9],
                                }
                            ),
                            "annotations": [],
                        }
                    }
                ]
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="deepseek/deepseek-v4-flash-0731",
        web_search=False,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )
    result = await enricher.analyze_story(
        story(1),
        [article(1, "Reuters"), article(2, "AP")],
        [story(9, "A directly connected prior event")],
    )
    assert result.related_story_ids == (9,)
    assert result.text.startswith("**Summary**\n")
    assert "**Key facts**" in result.text
    assert "Unclear or disputed" not in result.text
    assert "**Analysis sources**" in result.text
    assert "[Reuters](https://reuters.example/story)" in result.text
    assert "[AP](https://ap.example/story)" in result.text
    assert "\u2014" not in result.text
    await client.aclose()


async def test_story_analysis_uses_web_grounding_and_response_repair() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "tools" in body:
            assert body["tools"][0]["type"] == "openrouter:web_search"
            assert "response_format" not in body
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "A directly connected report confirms the event.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://research.example/report",
                                            "title": "Direct report",
                                            "content": "The event was confirmed.",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        supplied = json.loads(body["messages"][1]["content"])
        assert supplied["web_evidence"]["sources"][0]["url"] == ("https://research.example/report")
        assert "plugins" not in body
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "A report confirmed the event.",
                                    "key_facts": ["The event was reported."],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [],
                                }
                            ),
                            "annotations": [],
                        }
                    }
                ]
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="deepseek/deepseek-v4-flash-0731",
        web_search=True,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )
    result = await enricher.analyze_story(story(1), [article(1, "Wire")], [])
    assert len(requests) == 2
    assert "[research.example](https://research.example/report)" in result.text
    await client.aclose()


async def test_model_override_is_validated_and_applied_per_guild() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(
                200,
                json={"data": [{"id": "deepseek/deepseek-v4-flash-0731"}]},
            )
        body = json.loads(request.content)
        assert body["model"] == "deepseek/deepseek-v4-flash-0731"
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "A report confirmed the event.",
                                    "key_facts": ["The event was reported."],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [],
                                }
                            ),
                            "annotations": [],
                        }
                    }
                ]
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="openrouter/auto",
        web_search=False,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )
    model = await enricher.validate_model(" deepseek/deepseek-v4-flash-0731 ")
    enricher.set_model(1, model)
    assert enricher.model_for(1) == "deepseek/deepseek-v4-flash-0731"
    assert enricher.model_for(2) == "openrouter/auto"
    await enricher.analyze_story(story(1), [article(1, "Wire")], [])
    await client.aclose()


async def test_web_research_timeout_falls_back_to_feed_sources() -> None:
    calls = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.sleep(0.02)
            finally:
                enricher._request_timeout_seconds = 1
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "A report confirmed the event.",
                                    "key_facts": ["The event was reported."],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [],
                                }
                            ),
                            "annotations": [],
                        }
                    }
                ]
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="deepseek/deepseek-v4-flash-0731",
        web_search=True,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )
    enricher._request_timeout_seconds = 0.001
    result = await enricher.analyze_story(story(1), [article(1, "Wire")], [])
    assert calls == 2
    assert "A report confirmed the event." in result.text
    await client.aclose()


async def test_story_analysis_rejects_unknown_related_story_id() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "A policy changed.",
                                    "key_facts": ["One report describes the change."],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [999],
                                }
                            ),
                            "annotations": [],
                        }
                    }
                ]
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="openrouter/auto",
        web_search=False,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )
    with pytest.raises(EnrichmentError, match="outside the candidate list"):
        await enricher.analyze_story(story(1), [article(1, "Wire")], [story(9)])
    await client.aclose()
