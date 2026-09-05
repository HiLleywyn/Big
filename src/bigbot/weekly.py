from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from bigbot.domain import StoryState, WeeklyCandidate


@dataclass(frozen=True)
class RankedWeeklyStory:
    candidate: WeeklyCandidate
    score: int


_FORECAST_TERMS = (
    "expected to",
    "forecast",
    "preview",
    "outlook",
    "week ahead",
    "likely to",
    "could ",
    "may ",
    "set to",
    "poised to",
)

_ROUTINE_SPORTS_TERMS = (
    "diamond league",
    "tournament",
    "fixture",
    "playoff",
    "qualifier",
    "season opener",
    "pulls out",
    "withdraws",
    "back tightness",
    "injury update",
)

_CONFLICT_TERMS = (
    "airstrike",
    "attack",
    "ceasefire",
    "conflict",
    "invasion",
    "military",
    "missile",
    "nuclear",
    "sanction",
    "strike",
    "troops",
    "war ",
)

_DISASTER_TERMS = (
    "casualties",
    "deadly",
    "death toll",
    "earthquake",
    "evacuation",
    "explosion",
    "flood",
    "hurricane",
    "killed",
    "wildfire",
)

_GOVERNMENT_TERMS = (
    "congress",
    "court",
    "election",
    "government",
    "legislation",
    "parliament",
    "president",
    "prime minister",
    "regulation",
    "regulator",
    "referendum",
    "supreme court",
    "white house",
)

_MACRO_TERMS = (
    "central bank",
    "economy",
    "federal reserve",
    "gdp",
    "inflation",
    "interest rate",
    "jobs report",
    "recession",
    "tariff",
    "trade war",
    "unemployment",
)

_TECH_SECURITY_TERMS = (
    "artificial intelligence",
    "cyberattack",
    "cybersecurity",
    "data breach",
    "hack",
    "ransomware",
    "semiconductor",
)

_PUBLIC_HEALTH_TERMS = (
    "disease",
    "food safety",
    "health emergency",
    "outbreak",
    "pandemic",
    "public health",
)

_LOW_IMPACT_TERMS = (
    "commentary:",
    "appoints",
    "appointment",
    "celebrity",
    "fashion",
    "funding round",
    "full-year record",
    "morning bid",
    "name dispute",
    "newsletter",
    "opinion:",
    "podcast:",
    "recipe",
    "rumor",
    "trademark",
    "using 'twitter' name",
    'using "twitter" name',
    "valued at",
    "valuation",
    "viral video",
)

_NO_ACTION_TERMS = (
    "considers",
    "could ",
    "may ",
    "no decision",
    "reviewing",
    "reviews candidates",
    "sources say",
)

_SOURCE_TOKENS = {
    "ap",
    "com",
    "google",
    "news",
    "reuters",
}


def select_weekly_stories(
    candidates: list[WeeklyCandidate], *, limit: int
) -> list[WeeklyCandidate]:
    """Select consequential stories without rewarding syndicated duplicates."""
    if limit < 1:
        return []
    ranked = [rank_weekly_story(candidate) for candidate in candidates]
    eligible = [item for item in ranked if _is_eligible(item)]
    eligible.sort(
        key=lambda item: (
            item.score,
            item.candidate.source_count,
            item.candidate.story.last_updated_at,
            item.candidate.story.id,
        ),
        reverse=True,
    )
    selected: list[RankedWeeklyStory] = []
    for item in eligible:
        if any(_same_event(item.candidate, prior.candidate) for prior in selected):
            continue
        selected.append(item)
        if len(selected) == limit:
            break
    return [item.candidate for item in selected]


def rank_weekly_story(candidate: WeeklyCandidate) -> RankedWeeklyStory:
    story = candidate.story
    text = " ".join(value for value in (story.title, story.summary) if value).casefold()
    tags = {tag.casefold() for tag in story.tags}

    score = min(candidate.source_count, 4) * 16
    # Repeated copies from the same publisher add almost no editorial significance.
    duplicate_count = max(0, candidate.article_count - candidate.source_count)
    score += min(duplicate_count, 2)
    score += {
        StoryState.BREAKING: 24,
        StoryState.DEVELOPING: 14,
        StoryState.UPDATED: 8,
        StoryState.NEW: 4,
        StoryState.STALE: 0,
        StoryState.MERGED: -100,
    }[story.state]

    score += _signal_score(text, _CONFLICT_TERMS, 38)
    score += _signal_score(text, _DISASTER_TERMS, 42)
    score += _signal_score(text, _GOVERNMENT_TERMS, 24)
    score += _signal_score(text, _MACRO_TERMS, 20)
    score += _signal_score(text, _TECH_SECURITY_TERMS, 20)
    score += _signal_score(text, _PUBLIC_HEALTH_TERMS, 30)

    if any(term in text for term in _FORECAST_TERMS):
        score -= 44
    if "sports" in tags or any(term in text for term in _ROUTINE_SPORTS_TERMS):
        score -= 70
    if any(term in text for term in _LOW_IMPACT_TERMS):
        score -= 40
    if any(term in text for term in _NO_ACTION_TERMS):
        score -= 24

    return RankedWeeklyStory(candidate=candidate, score=score)


def is_publication_worthy(candidate: WeeklyCandidate) -> bool:
    """Use the same consequence-first standard for the live feed and weekly digest."""
    ranked = rank_weekly_story(candidate)
    story = candidate.story
    text = f"{story.title} {story.summary}".casefold()
    tags = {tag.casefold() for tag in story.tags}
    if any(term in text for term in _LOW_IMPACT_TERMS):
        return False
    if "sports" in tags or any(term in text for term in _ROUTINE_SPORTS_TERMS):
        return False
    if any(term in text for term in _FORECAST_TERMS) and candidate.source_count < 3:
        return False
    return ranked.score >= 20


def _signal_score(text: str, terms: tuple[str, ...], weight: int) -> int:
    matches = sum(term in text for term in terms)
    return min(matches, 2) * weight


def _is_eligible(ranked: RankedWeeklyStory) -> bool:
    story = ranked.candidate.story
    text = f"{story.title} {story.summary}".casefold()
    tags = {tag.casefold() for tag in story.tags}
    routine_sports = "sports" in tags or any(term in text for term in _ROUTINE_SPORTS_TERMS)
    forecast_only = any(term in text for term in _FORECAST_TERMS)
    independently_confirmed = ranked.candidate.source_count >= 3
    return (
        ranked.score >= 30 and not routine_sports and (not forecast_only or independently_confirmed)
    )


def _same_event(left: WeeklyCandidate, right: WeeklyCandidate) -> bool:
    left_title = _identity_title(left.story.normalized_title or left.story.title)
    right_title = _identity_title(right.story.normalized_title or right.story.title)
    if not left_title or not right_title:
        return False
    return SequenceMatcher(None, left_title, right_title).ratio() >= 0.6


def _identity_title(value: str) -> str:
    return " ".join(token for token in value.casefold().split() if token not in _SOURCE_TOKENS)
