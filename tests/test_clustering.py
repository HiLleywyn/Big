from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bigbot.clustering import DeterministicClusterer
from bigbot.domain import FeedItem, PublicationState, Story, StoryState
from bigbot.normalization import normalize_item

NOW = datetime.now(UTC)


def _normalized(title: str, summary: str = ""):
    return normalize_item(
        FeedItem(
            title, title, f"https://example.com/{abs(hash(title))}", summary or title, "AP", NOW
        ),
        fallback_publisher="AP",
    )


def _story(title: str, summary: str) -> Story:
    normalized = _normalized(title, summary)
    return Story(
        1,
        10,
        20,
        normalized.title,
        normalized.summary,
        StoryState.NEW,
        PublicationState.PUBLISHED,
        30,
        30,
        ("Markets",),
        normalized.normalized_title,
        normalized.entities,
        normalized.keywords,
        normalized.numbers,
        normalized.event_terms,
        NOW,
        NOW,
        NOW,
    )


@pytest.mark.parametrize(
    "headline",
    [
        "Fed lowers key interest rate by quarter point",
        "Federal Reserve announces 25-basis-point rate cut",
    ],
)
def test_paraphrased_coverage_clusters(headline: str) -> None:
    story = _story(
        "Federal Reserve cuts interest rates by 25 basis points",
        "The Federal Reserve lowered its benchmark rate after its policy meeting.",
    )
    decision = DeterministicClusterer().select(_normalized(headline), NOW, [story])
    assert decision.story == story
    assert decision.score >= 0.68


def test_conflicting_event_and_number_do_not_cluster() -> None:
    story = _story(
        "Federal Reserve cuts rates by 25 basis points",
        "The central bank lowered its benchmark rate.",
    )
    decision = DeterministicClusterer().select(
        _normalized("Federal Reserve raises rates by 50 basis points"), NOW, [story]
    )
    assert decision.story is None
    assert decision.signals["event_compatibility"] == -1.0


def test_same_broad_subject_different_event_stays_separate() -> None:
    story = _story(
        "Apple launches new iPhone",
        "Apple unveiled a new phone at its product event.",
    )
    decision = DeterministicClusterer().select(
        _normalized("Apple reports quarterly earnings"), NOW, [story]
    )
    assert decision.story is None


def test_threshold_is_validated() -> None:
    with pytest.raises(ValueError):
        DeterministicClusterer(0.2)
