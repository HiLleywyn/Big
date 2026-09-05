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
from bigbot.enrichment import (
    EnrichmentError,
    FactCheckVerdict,
    OpenRouterEnricher,
    _clean_fallback_candidate,
    _needs_web_evidence,
    _validate_result,
)


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


def test_fallback_summary_removes_reuters_page_chrome_and_stops_cleanly() -> None:
    value = (
        "By Thomson Reuters Sep 3, 2026 | 5:01 AM By Helen Coster NEW YORK, "
        "Sept 3 (Reuters) \u2013 Less than a year ago, officials supported the project. "
        "Now they are calling for new restrictions. Across the country, another "
        "unfinished sentence about the U.S."
    )

    result = _clean_fallback_candidate(value, title="Officials change course - Reuters")

    assert result == (
        "Less than a year ago, officials supported the project. "
        "Now they are calling for new restrictions."
    )


def test_fallback_summary_rejects_mid_sentence_excerpt() -> None:
    assert (
        _clean_fallback_candidate(
            "liability for officials who previously supported the project.",
            title="Officials change course - Reuters",
        )
        is None
    )


def test_fallback_summary_discards_mid_sentence_prefix() -> None:
    result = _clean_fallback_candidate(
        "liability for officials who previously supported the project. "
        "The governor then called for new restrictions.",
        title="Officials change course - Reuters",
    )

    assert result == "The governor then called for new restrictions."


def test_fallback_summary_rejects_research_process_text() -> None:
    assert (
        _clean_fallback_candidate(
            "I'll research this story. Let me get more details from the article.",
            title="Officials change course - Reuters",
        )
        is None
    )


def test_fallback_summary_rejects_share_and_video_navigation_chrome() -> None:
    value = (
        "(mailto:?subject=Iran latest&body=https%3A%2F%2Fexample.com&amp) Print Other Close "
        "Print Options Choose how you want to print Print with images Trump says US may hit "
        "Iran's Pickaxe Mountain soon 00:38 Another story 01:05 More video 03:11 News desk."
    )

    assert (
        _clean_fallback_candidate(
            value,
            title="Trump says US may hit Iran's Pickaxe Mountain soon - Reuters",
        )
        is None
    )


@pytest.mark.parametrize(
    "summary",
    [
        (
            "Page contents Top Quote(s) Related topics Print friendly pdf Contacts for media "
            "Today, the Commission adopted a proposal."
        ),
        (
            "SENSEX 76,645.76 492.90 NIFTY 23,910.90 37.45 CRUDEOIL 8,672.00 29.00 "
            "GOLD 154,939.00 THIS AD SUPPORTS OUR JOURNALISM. Home News World Nepal flood "
            "losses were estimated at $2.56 billion."
        ),
        (
            "AT A GLANCE Requested by the HOUS Special Committee Policy Department for "
            "Transport, Employment and Social Affairs Author: Claire Colomb Directorate-General "
            "for Cohesion, Agriculture and Social Policies PE 776.016. Access this note The "
            "regulatory aspects of short-term rentals in the EU."
        ),
    ],
)
def test_structured_analysis_rejects_page_navigation(summary: str) -> None:
    value = {
        "summary": summary,
        "key_facts": [],
        "useful_context": [],
        "unclear_or_disputed": [],
        "related_story_ids": [],
        "latest_update": None,
    }

    with pytest.raises(EnrichmentError, match="page chrome"):
        _validate_result(value, set())


def test_fallback_summary_removes_byline_and_repeated_headline() -> None:
    value = (
        "By Reuters September 4, 2026, 4:09:44 PM IST (Published) 3 Min Read Impact Shorts "
        "CNBCTV18 on Google Trump's bid to shield chip supply chain could backfire in "
        "Tennessee: Report A Tennessee factory may close after new trade measures drove away "
        "its two remaining customers. The facility employs about 600 workers."
    )

    result = _clean_fallback_candidate(
        value,
        title=(
            "EXCLUSIVE: Trump's bid to shield chip supply chain could backfire in Tennessee "
            "- Reuters"
        ),
    )

    assert result == (
        "A Tennessee factory may close after new trade measures drove away its two remaining "
        "customers. The facility employs about 600 workers."
    )


def test_structured_analysis_strips_wire_service_page_header_and_dateline() -> None:
    value = {
        "summary": (
            "Emirates News Agency Logo Emirates News Agency Nepal floods death toll reaches "
            "1,287 as losses hit $2.56 bn Emirates News Agency 2026-09-05T04:25:06+04:00 "
            "Nepal floods death toll reaches 1,287 as losses hit $2.56 bn KATHMANDU, 5th "
            "September, 2026 (WAM) -- Nepalese police said 1,287 people were killed and "
            "5,083 remained missing after floods and landslides."
        ),
        "key_facts": [],
        "useful_context": [],
        "unclear_or_disputed": [],
        "related_story_ids": [],
        "latest_update": None,
    }

    result = _validate_result(value, set())

    assert result.text == (
        "**Summary**\nNepalese police said 1,287 people were killed and 5,083 remained "
        "missing after floods and landslides."
    )


def test_structured_analysis_preserves_publication_decision() -> None:
    value = {
        "summary": "A startup announced a routine private funding round.",
        "key_facts": [],
        "useful_context": [],
        "unclear_or_disputed": [],
        "related_story_ids": [],
        "latest_update": None,
        "publication_suitable": False,
        "publication_reason": "A routine private funding announcement has limited public impact.",
    }

    result = _validate_result(value, set())

    assert not result.publication_suitable
    assert result.publication_reason == (
        "A routine private funding announcement has limited public impact."
    )


@pytest.mark.parametrize(
    "contaminated",
    [
        (
            "Notifications Explosions heard near Iran's Kharg Island Email Your Name "
            "Recipient Email Join our Whatsapp Channel Google Preferred Source "
            "COMMENT MOD POLICY Branded Content"
        ),
        (
            "Section: Russian air attacks killed 12 people and injured many more in Kyiv "
            "2 days ago Russian air attacks killed 12 people and injured many more in Kyiv "
            "1 day ago Russian air attacks killed 12 people and injured many more in Kyiv"
        ),
    ],
)
def test_structured_analysis_rejects_page_modules_and_repeated_results(
    contaminated: str,
) -> None:
    value = {
        "summary": contaminated,
        "key_facts": [],
        "useful_context": [],
        "unclear_or_disputed": [],
        "related_story_ids": [],
        "latest_update": None,
    }

    with pytest.raises(EnrichmentError, match="page chrome"):
        _validate_result(value, set())


@pytest.mark.parametrize(
    "summary",
    [
        (
            "On the day, the Nifty 50 opens new tab fell 0.43%. Skip to main content "
            "Exclusive news, data and analytics for financial market professionals Learn more "
            "about Refinitiv."
        ),
        (
            "Judge declares mistrial in Lindsay Clancy case Judge declares mistrial in Lindsay "
            "Clancy case Judge declares mistrial in Lindsay Clancy case. The jury was deadlocked."
        ),
        (
            "The jury was deadlocked. Posted September 4, 2026 Show more Top Videos Houston "
            "Texans Foundation celebrating 25 years during Season Premiere video."
        ),
        (
            "Rescuers pulled a survivor from the tunnel. 11:01 2 min Reading time Share "
            "Hundreds of workers remain missing."
        ),
        ("Off On Stream on stream logo Masked men blocked traffic near the port."),
        ("By Reuters Sep 2, 2026 Follow us Officials announced the change. ADVERTISEMENT"),
        (
            "By The Associated Press Updated September 5, 2026 1:58 am Share BEIJING - "
            "A mudslide killed one person and left 11 missing."
        ),
        (
            "Published at : August 26, 2026 Updated at : August 26, 2026 15:11 "
            "Nearly 400 pilgrims were out of contact after flooding."
        ),
    ],
)
def test_structured_analysis_rejects_search_result_and_video_modules(summary: str) -> None:
    value = {
        "summary": summary,
        "key_facts": [],
        "useful_context": [],
        "unclear_or_disputed": [],
        "related_story_ids": [],
        "latest_update": None,
    }

    with pytest.raises(EnrichmentError, match="page chrome"):
        _validate_result(value, set())


def test_structured_analysis_allows_repeated_single_market_name() -> None:
    value = {
        "summary": (
            "Gold rose 1% after the dollar weakened. Spot gold reached $4,376 an ounce. "
            "Gold futures for December delivery also settled higher."
        ),
        "key_facts": [],
        "useful_context": [],
        "unclear_or_disputed": [],
        "related_story_ids": [],
        "latest_update": None,
    }

    result = _validate_result(value, set())

    assert "Gold rose 1%" in result.text


def test_structured_analysis_rejects_lowercase_summary_opening() -> None:
    value = {
        "summary": "envoys arrived in Moscow to begin negotiations.",
        "key_facts": [],
        "useful_context": [],
        "unclear_or_disputed": [],
        "related_story_ids": [],
        "latest_update": None,
    }

    with pytest.raises(EnrichmentError, match="incomplete summary opening"):
        _validate_result(value, set())


def test_thin_article_description_requires_web_evidence() -> None:
    thin = article(1, "Wire")
    rich = replace(
        thin,
        description=(
            "Officials approved the measure after a public vote on Friday. The final tally "
            "was 18 to 7, with two members absent. The measure takes effect next month and "
            "requires agencies to publish quarterly implementation reports. Local governments "
            "have 90 days to update their procedures, and the oversight office will publish "
            "the first compliance review in December."
        ),
    )

    assert _needs_web_evidence((thin,))
    assert not _needs_web_evidence((rich,))


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
        story(1, "Officials announce policy change"),
        [article(1, "Reuters"), article(2, "AP")],
        [story(9, "Officials confirm policy change")],
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


async def test_story_analysis_rejects_unrelated_supplied_candidate() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Officials announced a policy change.",
                                    "key_facts": [],
                                    "useful_context": [],
                                    "unclear_or_disputed": [],
                                    "related_story_ids": [9],
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
        model="provider/model",
        web_search=False,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )

    result = await enricher.analyze_story(
        story(1, "Trump signs ranching order"),
        [article(1, "Reuters")],
        [story(9, "Russian drone strikes Ukraine security headquarters")],
    )

    assert result.related_story_ids == ()
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
    detailed_article = replace(
        article(1, "Wire"),
        description=(
            "Officials approved the measure after a public vote on Friday. The final tally "
            "was 18 to 7, with two members absent. The measure takes effect next month and "
            "requires agencies to publish quarterly implementation reports. Local governments "
            "have 90 days to update their procedures, and the oversight office will publish "
            "the first compliance review in December."
        ),
    )
    result = await enricher.analyze_story(story(1), [detailed_article], [])
    assert len(requests) == 1
    assert "[Wire](https://wire.example/story)" in result.text
    await client.aclose()


async def test_headline_only_story_researches_then_builds_grounded_summary() -> None:
    requests: list[dict[str, object]] = []
    cited_url = "https://example.gov/statement"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
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
                                "content": "An official statement confirms the allocation.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": cited_url,
                                            "title": "Official statement",
                                            "content": "Officials confirmed the allocation.",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        supplied = json.loads(body["messages"][1]["content"])
        assert "tools" not in body
        assert supplied["web_evidence"]["sources"][0]["url"] == cited_url
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": (
                                        "Officials confirmed the allocation at Thursday's event."
                                    ),
                                    "key_facts": [],
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
    sparse = article(1, "Wire")
    sparse = replace(sparse, description=sparse.title)

    result = await enricher.analyze_story(story(1), [sparse], [])

    assert len(requests) == 2
    assert "**Summary**" in result.text
    assert "Officials confirmed the allocation at Thursday's event" in result.text
    assert f"[example.gov]({cited_url})" in result.text
    assert "Key facts" not in result.text
    await client.aclose()


async def test_story_summary_accepts_provider_json_code_fence() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        content = json.dumps(
            {
                "summary": "Officials confirmed the allocation at Thursday's event.",
                "key_facts": [],
                "useful_context": [],
                "unclear_or_disputed": [],
                "related_story_ids": [],
                "latest_update": None,
            }
        )
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": f"```json\n{content}\n```"}}]},
        )

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="provider/model",
        web_search=False,
        zdr=True,
        timeout_seconds=10,
        client=client,
    )

    result = await enricher.analyze_story(story(1), [article(1, "Reuters")], [])

    assert "Officials confirmed the allocation" in result.text
    await client.aclose()


async def test_sparse_story_uses_grounded_research_when_structured_json_fails() -> None:
    calls = 0
    cited_url = "https://example.gov/statement"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        del request
        calls += 1
        if calls == 1:
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "Officials confirmed the allocation during Thursday's event. "
                                    "The fund contains about $1 billion."
                                ),
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://example.com/contaminated",
                                            "title": "Page chrome",
                                            "content": (
                                                "Industry, California, on June 2, 2026. "
                                                "Aaron Schwartz/Reuters Related coverage follows."
                                            ),
                                        },
                                    },
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://example.com/truncated",
                                            "title": "Truncated result",
                                            "content": (
                                                "Officials discussed the allocation ... More"
                                            ),
                                        },
                                    },
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": cited_url,
                                            "title": "Official statement",
                                            "content": "Officials confirmed the allocation.",
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "not valid json"}}]},
        )

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="provider/model",
        web_search=True,
        zdr=True,
        timeout_seconds=10,
        client=client,
    )
    source = article(1, "Reuters")
    sparse = replace(source, description=source.title)

    result = await enricher.analyze_story(story(1), [sparse], [])

    assert calls == 2
    assert "Officials confirmed the allocation" in result.text
    assert "... More" not in result.text
    assert f"[example.gov]({cited_url})" in result.text
    assert result.related_story_ids == ()
    await client.aclose()


async def test_sparse_story_rejects_structured_headline_rewrite() -> None:
    calls = 0
    cited_url = "https://example.gov/court-order"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        del request
        calls += 1
        if calls == 1:
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "The order temporarily prevents enforcement of the new "
                                    "ballot-envelope requirements while the case proceeds."
                                ),
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": cited_url,
                                            "title": "Court order",
                                            "content": (
                                                "The order temporarily prevents enforcement of "
                                                "the new ballot-envelope requirements while the "
                                                "case proceeds."
                                            ),
                                        },
                                    }
                                ],
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
                                    "summary": (
                                        "A US judge again blocked the Postal Service's "
                                        "mail-in voting restrictions."
                                    ),
                                    "key_facts": [
                                        "The judge's action was a repeat, as the title says."
                                    ],
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

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="provider/model",
        web_search=True,
        zdr=True,
        timeout_seconds=10,
        client=client,
    )
    source = replace(
        article(1, "Reuters"),
        title="US judge again blocks Postal Service's mail-in voting restrictions - Reuters",
        description="US judge again blocks Postal Service's mail-in voting restrictions Reuters",
    )
    target = replace(story(1), title=source.title, normalized_title=source.title.casefold())

    result = await enricher.analyze_story(target, [source], [])

    assert calls == 2
    assert "temporarily prevents enforcement" in result.text
    assert "action was a repeat" not in result.text
    assert f"[example.gov]({cited_url})" in result.text
    await client.aclose()


async def test_story_uses_source_description_when_structured_json_fails() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "not valid json"}}]},
        )

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="provider/model",
        web_search=False,
        zdr=True,
        timeout_seconds=10,
        client=client,
    )
    source = replace(
        article(1, "Reuters"),
        description="Officials said the measure takes effect Friday after lawmakers approved it.",
    )

    result = await enricher.analyze_story(story(1), [source], [])

    assert "**Summary**" in result.text
    assert "takes effect Friday" in result.text
    await client.aclose()


async def test_thin_story_uses_research_when_structured_summary_is_invalid() -> None:
    calls = 0
    cited_url = "https://example.gov/statement"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            assert body["tools"][0]["type"] == "openrouter:web_search"
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "Officials confirmed two additional measures on Friday."
                                ),
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": cited_url,
                                            "title": "Official statement",
                                            "content": (
                                                "Officials confirmed two additional measures."
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "not valid json"}}]},
        )

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="provider/model",
        web_search=True,
        zdr=True,
        timeout_seconds=10,
        client=client,
    )
    source = replace(article(1, "Reuters"), description="More details soon.")

    result = await enricher.analyze_story(story(1), [source], [])

    assert calls == 2
    assert "two additional measures" in result.text
    assert f"[example.gov]({cited_url})" in result.text
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


async def test_story_analysis_discards_unknown_related_story_id() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Officials approved the policy after a final vote.",
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
    result = await enricher.analyze_story(story(1), [article(1, "Wire")], [story(9)])

    assert result.related_story_ids == ()
    assert "Officials approved the policy" in result.text
    await client.aclose()
