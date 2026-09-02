from __future__ import annotations

import re
from dataclasses import dataclass

from bigbot.normalization import NormalizedArticle

TAG_CATALOG: tuple[str, ...] = (
    "Breaking",
    "Developing",
    "Politics",
    "World",
    "United States",
    "Markets",
    "Economy",
    "Business",
    "Technology",
    "AI",
    "Crypto",
    "Science",
    "Health",
    "Climate",
    "Security",
    "Law",
    "Culture",
    "Sports",
    "Fact Check",
    "General",
)

# These rules intentionally favor precision over recall. A feed's default tags provide
# the broad desk, while these rules add only categories supported by the story text.
DEFAULT_RULES: dict[str, tuple[str, ...]] = {
    "Breaking": ("breaking news", "breaking:", "urgent alert"),
    "Developing": (),
    "Politics": (
        "ballot",
        "brexit",
        "campaign",
        "commons",
        "congress",
        "downing street",
        "election",
        "government",
        "government shutdown",
        "parliament",
        "pmqs",
        "political party",
        "politics",
        "president",
        "prime minister",
        "senate",
    ),
    "World": (
        "ceasefire",
        "china",
        "diplomacy",
        "foreign ministry",
        "gaza",
        "hong kong",
        "international",
        "iran",
        "israel",
        "invasion",
        "nato",
        "russia",
        "sanctions",
        "united nations",
        "ukraine",
        "war",
    ),
    "United States": (
        "american",
        "federal government",
        "u.s.",
        "united states",
        "white house",
    ),
    "Markets": (
        "bond",
        "bonds",
        "dow jones",
        "futures",
        "investor",
        "investors",
        "market",
        "nasdaq",
        "s&p 500",
        "stock",
        "stocks",
        "trading",
        "yield",
        "yields",
    ),
    "Economy": (
        "central bank",
        "economic growth",
        "federal reserve",
        "gdp",
        "inflation",
        "interest rate",
        "jobs report",
        "recession",
        "unemployment",
    ),
    "Business": (
        "acquisition",
        "automaker",
        "bankruptcy",
        "chief executive",
        "earnings",
        "ipo",
        "merger",
        "oil deal",
        "revenue",
    ),
    "Technology": (
        "cloud computing",
        "electric vehicle",
        "hardware",
        "robot",
        "semiconductor",
        "self-flying",
        "smartphone",
        "software",
        "startup",
        "technology",
    ),
    "AI": (
        "ai model",
        "artificial intelligence",
        "chatbot",
        "deepseek",
        "machine learning",
        "openai",
    ),
    "Crypto": (
        "bitcoin",
        "blockchain",
        "crypto",
        "cryptocurrency",
        "defi",
        "ethereum",
        "stablecoin",
    ),
    "Science": (
        "astronomy",
        "discovery",
        "nasa",
        "physics",
        "researchers",
        "scientist",
        "spacecraft",
        "study finds",
    ),
    "Health": (
        "cdc",
        "cancer",
        "childbirth",
        "dementia",
        "diagnosis",
        "disease",
        "doctor",
        "drug trial",
        "health",
        "hospital",
        "medical",
        "newborn",
        "nhs",
        "outbreak",
        "patient",
        "prescription",
        "vaccine",
        "world health organization",
    ),
    "Climate": (
        "climate",
        "drought",
        "emissions",
        "flood",
        "floods",
        "heat wave",
        "hurricane",
        "hurricanes",
        "wildfire",
        "wildfires",
    ),
    "Security": (
        "breach",
        "cisa",
        "cyberattack",
        "cybersecurity",
        "exploit",
        "malware",
        "ransomware",
        "vulnerability",
    ),
    "Law": (
        "appeals court",
        "charged with",
        "convicted",
        "court ruling",
        "criminal trial",
        "guilty",
        "indictment",
        "juror",
        "jury",
        "justice department",
        "lawsuit",
        "murder",
        "police",
        "prison",
        "prisoner",
        "prosecutor",
        "regulation",
        "supreme court",
        "sexual assault",
        "stabbed",
    ),
    "Culture": (
        "art exhibition",
        "actor",
        "actress",
        "band",
        "book",
        "celebrity",
        "entertainment",
        "film",
        "gaming",
        "music",
        "programme",
        "singer",
        "song",
        "television",
        "tour",
    ),
    "Sports": (
        "baseball",
        "basketball",
        "championship",
        "coach",
        "cricket",
        "everton",
        "football",
        "grand slam",
        "league",
        "mlb",
        "nba",
        "nfl",
        "olympics",
        "player",
        "soccer",
        "sports",
        "striker",
        "tennis",
        "transfer",
        "wimbledon",
    ),
    "Fact Check": ("debunked", "fact check", "fact-check", "verified claim"),
    "General": (),
}


def _contains_term(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", text))


@dataclass(frozen=True)
class StoryClassifier:
    rules: dict[str, tuple[str, ...]]

    def classify(
        self, article: NormalizedArticle, *, feed_tags: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        title = article.title.casefold()
        description = article.summary.casefold()
        tags = list(dict.fromkeys(feed_tags))
        if "/sport/" in article.canonical_url.casefold() and "Sports" not in tags:
            tags.append("Sports")
        scored: list[tuple[int, int, str]] = []
        for order, (tag, terms) in enumerate(self.rules.items()):
            if tag in tags or not terms:
                continue
            title_hits = sum(_contains_term(title, term) for term in terms)
            description_hits = sum(_contains_term(description, term) for term in terms)
            score = title_hits * 3 + description_hits
            if score:
                scored.append((-score, order, tag))
        tags.extend(tag for _, _, tag in sorted(scored))
        if not tags:
            tags.append("General")
        return tuple(tags[:5])

    @classmethod
    def with_defaults(cls, overrides: dict[str, tuple[str, ...]] | None = None) -> StoryClassifier:
        rules = dict(DEFAULT_RULES)
        for tag, terms in (overrides or {}).items():
            rules[tag] = tuple(dict.fromkeys((*rules.get(tag, ()), *terms)))
        return cls(rules)
