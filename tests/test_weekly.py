from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bigbot.domain import (
    AnalysisState,
    PublicationState,
    Story,
    StoryState,
    WeeklyCandidate,
)
from bigbot.weekly import is_publication_worthy, rank_weekly_story, select_weekly_stories


def _story(
    story_id: int,
    title: str,
    *,
    summary: str = "",
    tags: tuple[str, ...] = (),
    state: StoryState = StoryState.DEVELOPING,
) -> Story:
    return Story(
        id=story_id,
        guild_id=1,
        forum_channel_id=2,
        title=title,
        summary=summary,
        state=state,
        publication_state=PublicationState.PUBLISHED,
        discord_thread_id=1000 + story_id,
        discord_starter_message_id=2000 + story_id,
        tags=tags,
        normalized_title=title.casefold(),
        entities=(),
        keywords=(),
        numbers=(),
        event_terms=(),
        first_published_at=datetime(2026, 9, 1, tzinfo=UTC),
        last_published_at=datetime(2026, 9, 1, tzinfo=UTC),
        last_updated_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=story_id),
        analysis_state=AnalysisState.READY,
    )


def _candidate(
    story_id: int,
    title: str,
    *,
    summary: str = "",
    tags: tuple[str, ...] = (),
    sources: int = 1,
    articles: int = 1,
) -> WeeklyCandidate:
    return WeeklyCandidate(
        story=_story(story_id, title, summary=summary, tags=tags),
        source_count=sources,
        article_count=articles,
    )


def test_weekly_selection_rejects_routine_sports_and_forecast_previews() -> None:
    candidates = [
        _candidate(
            1,
            "Duplantis pulls out of Diamond League Final with back tightness",
            tags=("Sports",),
            articles=4,
        ),
        _candidate(
            2,
            "US job growth expected to rebound; unemployment forecast steady",
            tags=("Economy",),
            articles=5,
        ),
        _candidate(3, "Russia hits Ukraine security headquarters in Kyiv"),
        _candidate(4, "Explosion at Bolivia military base kills 10 people"),
    ]

    selected = select_weekly_stories(candidates, limit=4)

    assert [candidate.story.id for candidate in selected] == [4, 3]


def test_live_quality_gate_rejects_routine_trademark_and_funding_blurbs() -> None:
    trademark = _candidate(
        10,
        "X Corp blocks startup from using Twitter name after trademark lawsuit",
    )
    funding = _candidate(
        11,
        "AI startup Crusoe valued at $30 billion after new funding round",
        tags=("Technology",),
    )

    assert not is_publication_worthy(trademark)
    assert not is_publication_worthy(funding)


@pytest.mark.parametrize(
    "title",
    [
        "COMMENTARY: Morning Bid: Bonds' reality check",
        "PODCAST: Waller's inflation signal",
        "OPINION: What markets should do next",
        "Daily markets newsletter",
        "South Korea exports surpass full-year record, hit $709.4 billion YTD",
    ],
)
def test_live_quality_gate_rejects_format_filler(title: str) -> None:
    assert not is_publication_worthy(_candidate(20, title, tags=("Markets",)))


def test_live_quality_gate_keeps_consequential_single_source_stories() -> None:
    flood = _candidate(12, "Nepal flood losses estimated at $2.56 billion")
    outbreak = _candidate(
        13,
        "Cyclosporiasis outbreak exposes weakened US public health system",
        tags=("Health",),
    )

    assert is_publication_worthy(flood)
    assert is_publication_worthy(outbreak)


def test_independent_sources_matter_more_than_same_publisher_duplicates() -> None:
    duplicated = _candidate(
        1,
        "Government releases routine departmental update",
        sources=1,
        articles=20,
    )
    confirmed = _candidate(
        2,
        "Government releases routine departmental update",
        sources=4,
        articles=4,
    )

    assert rank_weekly_story(confirmed).score > rank_weekly_story(duplicated).score + 30


def test_weekly_selection_is_bounded_and_deterministic() -> None:
    candidates = [
        _candidate(3, "Explosion at military base kills 10 people", sources=1),
        _candidate(2, "Government imposes sanctions after missile attack", sources=3),
        _candidate(1, "Supreme Court issues major election ruling", sources=2),
    ]

    first = select_weekly_stories(candidates, limit=2)
    second = select_weekly_stories(list(reversed(candidates)), limit=2)

    assert len(first) == 2
    assert [item.story.id for item in first] == [item.story.id for item in second]


def test_weekly_selection_does_not_repeat_same_event_as_separate_top_stories() -> None:
    candidates = [
        _candidate(
            1,
            "Russian drone strikes Ukraine security service headquarters in Kyiv",
            sources=2,
        ),
        _candidate(
            2,
            "Russia hits Ukraine security service HQ in Kyiv with drones",
            sources=1,
        ),
        _candidate(3, "Explosion at military base kills 10 people"),
    ]

    selected = select_weekly_stories(candidates, limit=3)

    assert len(selected) == 2
    assert {item.story.id for item in selected} == {1, 3}
