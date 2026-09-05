from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bigbot.domain import FeedItem
from bigbot.security import plain_text

TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
    "s",
    "spm",
}
TRACKING_PREFIXES = ("utm_", "vero_", "oly_")
STOPWORDS = {
    "a",
    "about",
    "after",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "new",
    "of",
    "on",
    "says",
    "that",
    "the",
    "their",
    "to",
    "was",
    "will",
    "with",
}
SYNONYMS = {
    "federal reserve": "fed",
    "quarter point": "25 bps",
    "quarter-point": "25 bps",
    "basis points": "bps",
    "basis point": "bps",
    "interest rates": "rates",
    "interest rate": "rate",
    "artificial intelligence": "ai",
    "cryptocurrency": "crypto",
    "united states": "us",
    "u s": "us",
}
EVENT_FORMS = {
    "approve": {"approve", "approves", "approved", "backs", "clears"},
    "attack": {"attack", "attacks", "attacked", "strike", "strikes", "struck"},
    "ban": {"ban", "bans", "banned", "block", "blocks", "blocked"},
    "cut": {"cut", "cuts", "lower", "lowers", "lowered", "reduce", "reduces", "reduced"},
    "die": {"die", "dies", "died", "dead", "killed"},
    "launch": {"launch", "launches", "launched", "unveil", "unveils", "unveiled"},
    "merge": {"acquire", "acquires", "acquired", "buy", "buys", "bought", "merge", "merges"},
    "raise": {"raise", "raises", "raised", "hike", "hikes", "hiked", "increase", "increases"},
    "reject": {"reject", "rejects", "rejected", "deny", "denies", "denied"},
    "resign": {"resign", "resigns", "resigned", "quit", "quits", "stepped"},
    "sue": {"sue", "sues", "sued", "lawsuit", "charges", "charged"},
    "win": {"win", "wins", "won", "elected", "reelected"},
}
EVENT_LOOKUP = {form: root for root, forms in EVENT_FORMS.items() for form in forms}
TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
ENTITY = re.compile(r"\b(?:[A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,3}|[A-Z]{2,})\b")
NUMBER = re.compile(r"\b\d+(?:\.\d+)?(?:%|\s?(?:bps|basis points?|million|billion|trillion))?\b")

_SOURCE_PAGE_MARKERS = (
    "access this note the regulatory aspects",
    "add al jazeera on google",
    "all content and metadata files",
    "at a glance requested by the",
    "bloomberg · bloomberg",
    "categories israel news",
    "choose how you want to print",
    "comment mod policy",
    "contacts for media",
    "content files pdf xml",
    "descriptive metadata (mods)",
    "directorate-general for",
    "email your name recipient email",
    "emirates news agency logo",
    "facebook email the associated press",
    "follow us",
    "google preferred source",
    "exclusive news, data and analytics",
    "learn more about refinitiv",
    "join our whatsapp channel",
    "latest from knkx",
    "latest news /",
    "mailto:",
    "metadata download",
    "notifications explosions heard",
    "off on stream on stream logo",
    "opens new tab",
    "page contents top quote",
    "policy department for",
    "preservation metadata (premis)",
    "published at :",
    "print friendly pdf",
    "print options",
    "print with images",
    "read more comments",
    "reading time share",
    "real estate listings",
    "socialsharebtn",
    "show more top videos",
    "skip to main content",
    "subscribe and get today's",
    "top videos",
    "successfully added",
    "this ad supports our journalism",
    "advertisement",
    "your name recipient email",
    "updated at :",
    "you are here home",
)


@dataclass(frozen=True)
class NormalizedArticle:
    title: str
    summary: str
    publisher: str
    canonical_url: str
    normalized_title: str
    title_tokens: frozenset[str]
    description_tokens: frozenset[str]
    entities: tuple[str, ...]
    keywords: tuple[str, ...]
    numbers: tuple[str, ...]
    event_terms: tuple[str, ...]
    fingerprint: str


def normalize_url(url: str) -> str:
    value = url.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower().rstrip(".")
    if not scheme or not hostname:
        return value
    port = parts.port
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if hostname == "news.google.com":
        path = re.sub(r"^/(?:rss|atom)/articles/", "/articles/", path)
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, item_value)
        for key, item_value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMETERS and not key.lower().startswith(TRACKING_PREFIXES)
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def normalize_headline(value: str) -> str:
    text = unicodedata.normalize("NFKC", plain_text(value, limit=500)).lower()
    text = re.sub(
        r"\b(\d+(?:\.\d+)?)\s*-\s*basis\s*-\s*points?\b",
        r"\1 bps",
        text,
    )
    text = re.sub(r"\b(\d+(?:\.\d+)?)\s+basis\s+points?\b", r"\1 bps", text)
    for source, replacement in SYNONYMS.items():
        text = text.replace(source, replacement)
    words = [EVENT_LOOKUP.get(word, word) for word in TOKEN.findall(text)]
    return " ".join(word for word in words if word not in STOPWORDS)


def contains_source_artifacts(value: str) -> bool:
    """Identify navigation, metadata, ads, and repeated page modules in scraped copy."""
    lowered = re.sub(r"\s+", " ", value.casefold()).strip()
    if not lowered:
        return False
    if any(marker in lowered for marker in _SOURCE_PAGE_MARKERS):
        return True
    if len(re.findall(r"\b\d{1,2}:\d{2}\b", lowered)) >= 3:
        return True
    if "(published)" in lowered and bool(re.search(r"\b\d+\s+min read\b", lowered)):
        return True
    if re.match(r"^by (?:reuters|the associated press|associated press|ap)\b", lowered) and any(
        marker in lowered for marker in (" advertisement", " follow us", " share ")
    ):
        return True
    if bool(re.search(r"\b\d+\s+min(?:ute)?s?\s+(?:read|reading time)\b", lowered)) and (
        "advertising" in lowered or "read more" in lowered
    ):
        return True
    if "section:" in lowered and len(re.findall(r"\b\d+\s+days?\s+ago\b", lowered)) >= 2:
        return True
    market_symbols = set(re.findall(r"\b(?:sensex|nifty|crudeoil|gold|silver)\b", lowered))
    if len(market_symbols) >= 3:
        return True
    words = re.findall(r"[a-z0-9]+", lowered)
    if any(
        words[:size] == words[size : size * 2] for size in range(4, min(13, len(words) // 2 + 1))
    ):
        return True
    if len(words) >= 18:
        shingles = Counter(tuple(words[index : index + 5]) for index in range(len(words) - 4))
        if any(count >= 3 for count in shingles.values()):
            return True
    return False


def tokenize(value: str) -> frozenset[str]:
    return frozenset(
        word for word in normalize_headline(value).split() if len(word) > 1 or word.isdigit()
    )


def extract_entities(title: str, summary: str) -> tuple[str, ...]:
    values: set[str] = set()
    for match in ENTITY.findall(f"{title}. {summary}"):
        normalized = normalize_headline(match)
        if normalized and normalized not in STOPWORDS:
            values.add(normalized)
    return tuple(sorted(values))


def extract_keywords(
    title_tokens: frozenset[str], description_tokens: frozenset[str]
) -> tuple[str, ...]:
    ranked = sorted(
        title_tokens | description_tokens,
        key=lambda token: (token not in title_tokens, -len(token), token),
    )
    return tuple(ranked[:16])


def normalize_item(item: FeedItem, *, fallback_publisher: str) -> NormalizedArticle:
    title = plain_text(item.title, limit=500) or "Untitled story"
    candidate_summary = plain_text(item.summary, limit=4000)
    summary = (
        candidate_summary
        if candidate_summary and not contains_source_artifacts(candidate_summary)
        else title
    )
    title_tokens = tokenize(title)
    description_tokens = tokenize(summary)
    normalized_title = " ".join(sorted(title_tokens))
    publisher = plain_text(item.publisher or item.author or fallback_publisher, limit=200)
    canonical_url = normalize_url(item.url)
    entities = extract_entities(title, summary)
    keywords = extract_keywords(title_tokens, description_tokens)
    numbers = tuple(sorted(set(NUMBER.findall(normalize_headline(f"{title} {summary}")))))
    event_terms = tuple(sorted({token for token in title_tokens if token in EVENT_FORMS}))
    material = "\n".join((publisher.casefold(), normalized_title, " ".join(keywords)))
    fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return NormalizedArticle(
        title=title,
        summary=summary,
        publisher=publisher,
        canonical_url=canonical_url,
        normalized_title=normalized_title,
        title_tokens=title_tokens,
        description_tokens=description_tokens,
        entities=entities,
        keywords=keywords,
        numbers=numbers,
        event_terms=event_terms,
        fingerprint=fingerprint,
    )
