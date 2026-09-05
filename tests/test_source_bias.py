from bigbot.source_bias import (
    canonical_source_name,
    source_bias_bucket,
    source_bias_score,
)


def test_known_outlets_are_normalized_and_bucketed() -> None:
    assert canonical_source_name("reuters.com", "https://www.reuters.com/world/") == "Reuters"
    assert source_bias_bucket("Reuters", "https://reuters.com/world/") == "center"
    assert source_bias_bucket("AP News", "https://apnews.com/article/example") == "left"
    assert (
        canonical_source_name('"site:reuters.com" - Google News', "https://news.google.com/rss")
        == "Reuters"
    )
    assert source_bias_bucket("NPR Topics: News", "https://npr.org/example") == "center-left"
    assert source_bias_score("Fox News", "https://foxnews.com/example") == 2
    assert source_bias_bucket("The Wall Street Journal", "https://wsj.com/news") == "center"


def test_official_and_unrated_sources_are_not_forced_left_or_right() -> None:
    assert source_bias_bucket("Federal Register", "https://federalregister.gov/doc") == "official"
    assert source_bias_bucket("Local Independent", "https://local.example/story") == "unrated"
    assert source_bias_score("Local Independent", "https://local.example/story") is None
