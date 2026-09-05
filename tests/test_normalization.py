from __future__ import annotations

from datetime import UTC, datetime

from bigbot.domain import FeedItem
from bigbot.normalization import (
    contains_source_artifacts,
    normalize_headline,
    normalize_item,
    normalize_url,
)


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


def test_page_navigation_and_unrelated_ad_copy_are_rejected() -> None:
    value = (
        "Notifications Explosions heard near Iran's Kharg Island. Email Your Name Recipient "
        "Email Cancel 0 comments Join our Whatsapp Channel Add Dawn as a trusted source "
        "Google Preferred Source Read more Comments 500 characters COMMENT MOD POLICY "
        "Branded Content Meydan Homes reimagines community living."
    )

    assert contains_source_artifacts(value)


def test_article_page_headers_and_read_more_modules_are_rejected() -> None:
    assert contains_source_artifacts(
        "LATEST NEWS / Middle East You are here Home EMAIL ALERTS Stay on top of the issues"
    )
    assert contains_source_artifacts(
        "00:04 2 min Reading time Story details Advertising Read more Additional copy"
    )


def test_repeated_search_result_modules_are_rejected() -> None:
    module = (
        "Russian air attacks killed 12 people and injured many more in Kyiv and the "
        "surrounding region early on Tuesday authorities said"
    )

    assert contains_source_artifacts(f"Section: {module} 2 days ago {module} 1 day ago {module}")


def test_normalization_discards_contaminated_feed_description() -> None:
    item = FeedItem(
        "bad",
        "Explosions heard near Kharg Island",
        "https://example.com/bad",
        "Email Your Name Recipient Email Join our Whatsapp Channel Google Preferred Source",
        "Wire",
        datetime.now(UTC),
    )

    normalized = normalize_item(item, fallback_publisher="Wire")

    assert normalized.summary == item.title
