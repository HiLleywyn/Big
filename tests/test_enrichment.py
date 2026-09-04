from __future__ import annotations

import json
from dataclasses import replace
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
from bigbot.enrichment import EnrichmentError, FactCheckVerdict, OpenRouterEnricher


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
        assert body["reasoning"] == {"effort": "minimal", "exclude": True}
        supplied = json.loads(body["messages"][1]["content"])
        assert [item["publisher"] for item in supplied["articles"]] == ["Reuters", "AP"]
        assert set(supplied["articles"][0]) == {
            "title",
            "description",
            "publisher",
            "url",
            "published_at",
            "article_id",
        }
        assert supplied["focus_article_id"] is None
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
                                    "useful_context": ["The change follows the latest meeting."],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [9],
                                    "latest_update": "Officials confirmed the policy change.",
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
    assert result.latest_update == "Officials confirmed the policy change."
    assert result.text.startswith("**Summary**\n")
    assert "**Key facts**" in result.text
    assert "Unclear or disputed" not in result.text
    assert "**Sources**" in result.text
    assert "[Reuters](https://reuters.example/story)" in result.text
    assert "[AP](https://ap.example/story)" in result.text
    assert "\u2014" not in result.text
    await client.aclose()


async def test_fact_check_researches_claims_and_validates_evidence_links() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if "tools" in body:
            assert body["tools"][0]["type"] == "openrouter:web_search"
            assert body["max_tool_calls"] == 5
            supplied = json.loads(body["messages"][1]["content"])
            assert supplied["selected_message"] == ("The measure rose 3 percent. Best result ever!")
            assert supplied["earlier_author_messages"] == ["The monthly release is out."]
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "The official release reports a 3 percent increase.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://agency.example/release",
                                            "title": "Official release",
                                            "content": "The measure increased 3 percent.",
                                        },
                                    },
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://wire.example/report",
                                            "title": "Independent report",
                                            "content": "The release reports a 3 percent increase.",
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                },
            )
        supplied = json.loads(body["messages"][1]["content"])
        assert supplied["selected_message"] == "The measure rose 3 percent. Best result ever!"
        assert supplied["earlier_author_messages"] == ["The monthly release is out."]
        assert supplied["allowed_source_urls"] == [
            "https://agency.example/release",
            "https://wire.example/report",
        ]
        assert body["response_format"]["json_schema"]["strict"] is True
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "claims": [
                                        {
                                            "claim": "The measure rose 3 percent.",
                                            "verdict": "True",
                                            "explanation": (
                                                "The official release reports the same increase, "
                                                "and independent reporting matches it."
                                            ),
                                            "source_urls": [
                                                "https://agency.example/release",
                                                "https://wire.example/report",
                                            ],
                                        }
                                    ]
                                }
                            )
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
    result = await enricher.fact_check(
        guild_id=1,
        message_text="The measure rose 3 percent. Best result ever!",
        message_urls=(),
        context_messages=("The monthly release is out.",),
    )
    assert len(requests) == 2
    assert len(result.claims) == 1
    assert result.claims[0].verdict is FactCheckVerdict.TRUE
    assert [source.label for source in result.claims[0].sources] == [
        "agency.example",
        "wire.example",
    ]
    await client.aclose()


async def test_fact_check_rejects_source_not_returned_by_search() -> None:
    calls = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "No reliable evidence found.",
                                "annotations": [],
                            }
                        }
                    ]
                },
            )
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "claims": [
                                        {
                                            "claim": "A factual claim.",
                                            "verdict": "Unsupported",
                                            "explanation": "No reliable evidence establishes it.",
                                            "source_urls": ["https://invented.example/evidence"],
                                        }
                                    ]
                                }
                            )
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
    with pytest.raises(EnrichmentError, match="unverified source URL"):
        await enricher.fact_check(
            guild_id=1,
            message_text="A factual claim.",
            message_urls=(),
        )
    await client.aclose()


async def test_single_question_allows_only_one_fact_check_verdict() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if "tools" in body:
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "A dictionary records the term's history.",
                                "annotations": [],
                            }
                        }
                    ]
                },
            )
        schema = body["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["claims"]["maxItems"] == 1
        assert "Earlier messages are context only" in body["messages"][0]["content"]
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "claims": [
                                        {
                                            "claim": "The term was coined by white people.",
                                            "verdict": "Unclear",
                                            "explanation": "The available evidence is incomplete.",
                                            "source_urls": [],
                                        }
                                    ]
                                }
                            )
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
    result = await enricher.fact_check(
        guild_id=1,
        message_text="Was the term coined by white people?",
        message_urls=(),
        context_messages=("We were discussing the history of the term.",),
    )
    assert len(result.claims) == 1
    await client.aclose()


async def test_story_summary_uses_one_request_when_web_search_is_enabled() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        supplied = json.loads(body["messages"][1]["content"])
        assert "web_evidence" not in supplied
        assert "tools" not in body
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
                                    "useful_context": [],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [],
                                    "latest_update": None,
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
    assert len(requests) == 1
    assert "[Wire](https://wire.example/story)" in result.text
    await client.aclose()


async def test_headline_only_story_uses_bounded_web_search_and_citations() -> None:
    requests: list[dict[str, object]] = []
    cited_url = "https://example.gov/statement"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body["tools"] == [
            {
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": "parallel",
                    "mode": "turbo",
                    "max_results": 4,
                    "max_uses": 2,
                    "max_total_results": 6,
                    "max_characters": 2000,
                },
            }
        ]
        assert body["max_tool_calls"] == 2
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Officials confirmed the allocation on Thursday.",
                                    "key_facts": [],
                                    "useful_context": [],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [],
                                    "latest_update": None,
                                }
                            ),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {"url": cited_url},
                                }
                            ],
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
    sparse = article(1, "Wire")
    sparse = replace(sparse, description=sparse.title)

    result = await enricher.analyze_story(story(1), [sparse], [])

    assert len(requests) == 1
    assert "Officials confirmed the allocation" in result.text
    assert f"[example.gov]({cited_url})" in result.text
    assert "Key facts" not in result.text
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
                                    "useful_context": [],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [],
                                    "latest_update": None,
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
                                    "useful_context": [],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [999],
                                    "latest_update": None,
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
