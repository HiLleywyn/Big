from __future__ import annotations

from dataclasses import dataclass

from bigbot.normalization import NormalizedArticle

DEFAULT_RULES: dict[str, tuple[str, ...]] = {
    "Politics": ("election", "government", "minister", "president", "senate", "vote"),
    "World": ("country", "diplomat", "global", "international", "nation", "war"),
    "Markets": ("bond", "fed", "market", "rate", "stock", "trading"),
    "Business": ("business", "company", "earnings", "merge", "revenue"),
    "Technology": ("ai", "chip", "software", "startup", "technology"),
    "Crypto": ("bitcoin", "blockchain", "crypto", "ethereum", "token"),
    "Science": ("climate", "research", "science", "space", "study"),
    "Security": ("breach", "cyber", "hack", "malware", "security"),
    "Breaking": ("breaking", "urgent"),
    "Developing": ("developing", "live", "update"),
}


@dataclass(frozen=True)
class StoryClassifier:
    rules: dict[str, tuple[str, ...]]

    def classify(
        self, article: NormalizedArticle, *, feed_tags: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        haystack = set(article.title_tokens) | set(article.description_tokens)
        tags = list(feed_tags)
        for tag, terms in self.rules.items():
            if any(term.casefold() in haystack for term in terms) and tag not in tags:
                tags.append(tag)
        return tuple(tags[:5])

    @classmethod
    def with_defaults(cls, overrides: dict[str, tuple[str, ...]] | None = None) -> StoryClassifier:
        rules = dict(DEFAULT_RULES)
        rules.update(overrides or {})
        return cls(rules)
