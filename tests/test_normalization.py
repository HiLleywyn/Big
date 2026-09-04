from __future__ import annotations

from datetime import UTC, datetime

from bigbot.domain import FeedItem
from bigbot.normalization import normalize_headline, normalize_item, normalize_url


def test_url_normalization_removes_tracking_and_normalizes_shape() -> None:
    first = normalize_url(
        "HTTPS://Example.COM:443/news/story/?utm_source=x&b=2&a=1&fbclid=ignored#fragment"
    )
    second = normalize_url("https://example.com/news/story?a=1&b=2")
    assert first == second == "https://example.com/news/story?a=1&b=2"


def test_google_news_rss_and_atom_article_urls_are_the_same_article() -> None:
    rss = normalize_url("https://news.google.com/rss/articles/ABC123?oc=5")
    atom = normalize_url("https://news.google.com/atom/articles/ABC123?oc=5")
    assert rss == atom == "https://news.google.com/articles/ABC123?oc=5"


def test_headline_normalization_handles_common_news_paraphrases() -> None:
    assert normalize_headline("Federal Reserve lowers interest rates by a quarter point") == (
        "fed cut rates 25 bps"
    )


def test_normalized_article_extracts_entities_numbers_and_events() -> None:
    item = FeedItem(
        "one",
        "Federal Reserve cuts rates by 25 basis points",
        "https://example.com/one",
        "Federal Reserve officials voted after the policy meeting.",
        "Reuters",
        datetime.now(UTC),
    )
    normalized = normalize_item(item, fallback_publisher="Wire")
    assert "fed" in normalized.entities
    assert "25 bps" in normalized.numbers
    assert normalized.event_terms == ("cut",)
