from __future__ import annotations

import json

import httpx2
import pytest

from bigbot.domain import FeedItem
from bigbot.enrichment import EnrichmentError, OpenRouterEnricher


def item() -> FeedItem:
    return FeedItem(
        "1",
        "A disputed policy claim",
        "https://example.com/original",
        "The source claims a policy changed outcomes by 10%.",
        "Wire",
        None,
    )


async def test_enrichment_requires_and_renders_citations() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        assert body["provider"] == {"data_collection": "deny", "zdr": True}
        assert body["tools"][0]["type"] == "openrouter:web_search"
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "**Summary**\nA concise account.\n\n**Context**\nA sourced fact."
                            ),
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {"url": "https://data.example.org/report"},
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
        model="openrouter/auto",
        web_search=True,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )
    result = await enricher.enrich(item())
    assert "AI briefing" in result.text
    assert "https://example.com/original" in result.text
    assert "https://data.example.org/report" in result.text
    assert len(result.text) <= 2000
    await client.aclose()


async def test_enrichment_refuses_unsourced_web_output() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "Unsupported context", "annotations": []}}]},
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    enricher = OpenRouterEnricher(
        api_key="secret",
        model="openrouter/auto",
        web_search=True,
        zdr=True,
        timeout_seconds=15,
        client=client,
    )
    with pytest.raises(EnrichmentError, match="no verifiable web citations"):
        await enricher.enrich(item())
    await client.aclose()
