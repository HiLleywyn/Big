from __future__ import annotations

from datetime import UTC, datetime

from bigbot.classification import TAG_CATALOG, StoryClassifier
from bigbot.domain import FeedItem
from bigbot.normalization import normalize_item


def _article(title: str, summary: str = "", url: str = "https://example.com/story"):
    return normalize_item(
        FeedItem("id", title, url, summary, "Wire", datetime.now(UTC)),
        fallback_publisher="Wire",
    )


def test_catalog_fits_discord_limit_and_uses_unique_names() -> None:
    assert len(TAG_CATALOG) == 20
    assert len({name.casefold() for name in TAG_CATALOG}) == 20
    assert all(0 < len(name) <= 20 for name in TAG_CATALOG)


def test_classification_uses_phrases_and_title_weighting() -> None:
    tags = StoryClassifier.with_defaults().classify(
        _article(
            "Federal Reserve cuts interest rate after inflation report",
            "Investors sent stocks higher after the central bank decision.",
        )
    )
    assert tags[:2] == ("Economy", "Markets")


def test_short_terms_do_not_match_inside_unrelated_words() -> None:
    tags = StoryClassifier.with_defaults().classify(
        _article("Music festival announces summer lineup")
    )
    assert "AI" not in tags
    assert "United States" not in tags


def test_feed_default_tag_is_preserved_without_duplicates() -> None:
    tags = StoryClassifier.with_defaults().classify(
        _article("Bitcoin market rises after new filing"), feed_tags=("Crypto", "Crypto")
    )
    assert tags[0] == "Crypto"
    assert tags.count("Crypto") == 1


def test_source_route_can_supply_a_precise_category() -> None:
    tags = StoryClassifier.with_defaults().classify(
        _article(
            "Deadline day move completed",
            url="https://www.bbc.co.uk/sport/football/articles/example",
        )
    )
    assert tags == ("Sports",)
