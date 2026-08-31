from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Protocol

from bigbot.domain import Story
from bigbot.normalization import NormalizedArticle


@dataclass(frozen=True)
class ClusterDecision:
    story: Story | None
    score: float
    significant_update: bool
    signals: dict[str, float]


class StoryClusterer(Protocol):
    def select(
        self, article: NormalizedArticle, published_at: datetime, candidates: list[Story]
    ) -> ClusterDecision: ...


class DeterministicClusterer:
    def __init__(self, threshold: float = 0.68) -> None:
        if not 0.5 <= threshold <= 0.95:
            raise ValueError("clustering threshold must be between 0.5 and 0.95")
        self.threshold = threshold

    def select(
        self, article: NormalizedArticle, published_at: datetime, candidates: list[Story]
    ) -> ClusterDecision:
        best_story: Story | None = None
        best_score = 0.0
        best_signals: dict[str, float] = {}
        for story in candidates:
            score, signals = self.score(article, published_at, story)
            if score > best_score:
                best_story, best_score, best_signals = story, score, signals
        if best_story is None or best_score < self.threshold or not self._has_support(best_signals):
            return ClusterDecision(None, best_score, False, best_signals)
        significant = (
            best_signals.get("novelty", 0.0) >= 0.38 or best_signals.get("new_facts", 0.0) >= 0.5
        )
        return ClusterDecision(best_story, best_score, significant, best_signals)

    def score(
        self, article: NormalizedArticle, published_at: datetime, story: Story
    ) -> tuple[float, dict[str, float]]:
        article_title = article.title_tokens
        story_title = frozenset(story.normalized_title.split())
        headline_jaccard = _jaccard(article_title, story_title)
        headline_sequence = SequenceMatcher(
            None, " ".join(sorted(article_title)), " ".join(sorted(story_title))
        ).ratio()
        entity_overlap = _jaccard(set(article.entities), set(story.entities))
        keyword_overlap = _jaccard(set(article.keywords), set(story.keywords))
        description_similarity = _cosine(article.description_tokens, set(story.keywords))
        hours = (
            abs((published_at - (story.last_published_at or published_at)).total_seconds()) / 3600
        )
        time_proximity = max(0.0, 1.0 - hours / 96.0)
        event_compatibility = _compatibility(set(article.event_terms), set(story.event_terms))
        number_compatibility = _compatibility(set(article.numbers), set(story.numbers))
        base = (
            0.29 * headline_jaccard
            + 0.21 * headline_sequence
            + 0.17 * entity_overlap
            + 0.13 * keyword_overlap
            + 0.12 * description_similarity
            + 0.08 * time_proximity
        )
        if event_compatibility < 0:
            base -= 0.22
        elif event_compatibility > 0:
            base += 0.07
        if number_compatibility < 0:
            base -= 0.13
        elif number_compatibility > 0:
            base += 0.04
        novelty = 1.0 - max(headline_jaccard, keyword_overlap)
        new_facts = float(
            bool(set(article.numbers) - set(story.numbers))
            or bool(set(article.entities) - set(story.entities))
        )
        signals = {
            "headline": headline_jaccard,
            "sequence": headline_sequence,
            "entities": entity_overlap,
            "keywords": keyword_overlap,
            "description": description_similarity,
            "time": time_proximity,
            "event_compatibility": event_compatibility,
            "number_compatibility": number_compatibility,
            "novelty": novelty,
            "new_facts": new_facts,
        }
        return max(0.0, min(1.0, base)), signals

    @staticmethod
    def _has_support(signals: dict[str, float]) -> bool:
        if signals.get("event_compatibility", 0.0) < 0:
            return False
        headline = max(signals.get("headline", 0.0), signals.get("sequence", 0.0))
        entities = signals.get("entities", 0.0)
        keywords = signals.get("keywords", 0.0)
        description = signals.get("description", 0.0)
        return (headline >= 0.58 and (entities >= 0.25 or keywords >= 0.4)) or (
            headline >= 0.72 and description >= 0.2
        )


def _jaccard(left: set[str] | frozenset[str], right: set[str] | frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _cosine(left: set[str] | frozenset[str], right: set[str] | frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    left_counts = Counter(left)
    right_counts = Counter(right)
    dot = sum(left_counts[token] * right_counts[token] for token in left_counts & right_counts)
    left_norm = math.sqrt(sum(value * value for value in left_counts.values()))
    right_norm = math.sqrt(sum(value * value for value in right_counts.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _compatibility(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 1.0 if left & right else -1.0
