from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Protocol

from bigbot.domain import Article, Story
from bigbot.normalization import NormalizedArticle


@dataclass(frozen=True)
class ClusterDecision:
    story: Story | None
    score: float
    significant_update: bool
    signals: dict[str, float]


@dataclass(frozen=True)
class ClusterOutlier:
    article: Article
    score: float
    reason: str


class StoryClusterer(Protocol):
    def select(
        self, article: NormalizedArticle, published_at: datetime, candidates: list[Story]
    ) -> ClusterDecision: ...

    def story_merge_score(
        self, left_articles: list[Article], right_articles: list[Article]
    ) -> float: ...

    def find_outlier(
        self, articles: list[Article], *, threshold: float
    ) -> ClusterOutlier | None: ...


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

    def score_articles(self, left: Article, right: Article) -> tuple[float, dict[str, float]]:
        left_title = frozenset(left.normalized_title.split())
        right_title = frozenset(right.normalized_title.split())
        headline_jaccard = _jaccard(left_title, right_title)
        headline_sequence = SequenceMatcher(
            None, " ".join(sorted(left_title)), " ".join(sorted(right_title))
        ).ratio()
        entity_overlap = _jaccard(set(left.entities), set(right.entities))
        keyword_overlap = _jaccard(set(left.keywords), set(right.keywords))
        description_similarity = _cosine(set(left.keywords), set(right.keywords))
        published_left = left.published_at or left.discovered_at
        published_right = right.published_at or right.discovered_at
        hours = abs((published_left - published_right).total_seconds()) / 3600
        time_proximity = max(0.0, 1.0 - hours / 96.0)
        event_compatibility = _compatibility(set(left.event_terms), set(right.event_terms))
        number_compatibility = _compatibility(set(left.numbers), set(right.numbers))
        score = (
            0.29 * headline_jaccard
            + 0.21 * headline_sequence
            + 0.17 * entity_overlap
            + 0.13 * keyword_overlap
            + 0.12 * description_similarity
            + 0.08 * time_proximity
        )
        if event_compatibility < 0:
            score -= 0.22
        elif event_compatibility > 0:
            score += 0.07
        if number_compatibility < 0:
            score -= 0.13
        elif number_compatibility > 0:
            score += 0.04
        signals = {
            "headline": headline_jaccard,
            "sequence": headline_sequence,
            "entities": entity_overlap,
            "keywords": keyword_overlap,
            "description": description_similarity,
            "time": time_proximity,
            "event_compatibility": event_compatibility,
            "number_compatibility": number_compatibility,
        }
        return max(0.0, min(1.0, score)), signals

    def story_merge_score(
        self, left_articles: list[Article], right_articles: list[Article]
    ) -> float:
        if not left_articles or not right_articles:
            return 0.0
        smaller, larger = (
            (left_articles, right_articles)
            if len(left_articles) <= len(right_articles)
            else (right_articles, left_articles)
        )
        supported_scores: list[float] = []
        for article in smaller:
            matches = [
                score
                for candidate in larger
                for score, signals in [self.score_articles(article, candidate)]
                if self._has_support(signals)
            ]
            if not matches:
                return 0.0
            supported_scores.append(max(matches))
        return min(supported_scores)

    def find_outlier(self, articles: list[Article], *, threshold: float) -> ClusterOutlier | None:
        if len(articles) < 2:
            return None
        candidates: list[ClusterOutlier] = []
        for article in articles:
            comparisons = [
                self.score_articles(article, candidate)
                for candidate in articles
                if candidate.id != article.id
            ]
            supported = [score for score, signals in comparisons if self._has_support(signals)]
            if supported and max(supported) >= threshold:
                continue
            best_score = max((score for score, _ in comparisons), default=0.0)
            event_conflicts = sum(
                signals.get("event_compatibility", 0.0) < 0 for _, signals in comparisons
            )
            low_identity = all(
                max(signals.get("headline", 0.0), signals.get("sequence", 0.0)) < 0.42
                and signals.get("entities", 0.0) < 0.25
                for _, signals in comparisons
            )
            if event_conflicts == len(comparisons):
                reason = "conflicting event terms"
            elif best_score < threshold and low_identity:
                reason = "no shared event identity"
            else:
                continue
            candidates.append(ClusterOutlier(article, best_score, reason))
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: (candidate.score, -candidate.article.id))

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
