from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SourceProfile:
    name: str
    bias: str
    score: int
    aliases: tuple[str, ...]
    domains: tuple[str, ...]


_PROFILES = (
    SourceProfile(
        "Associated Press", "center", 0, ("ap", "ap news", "associated press"), ("apnews.com",)
    ),
    SourceProfile("Reuters", "center", 0, ("reuters", "reuters.com"), ("reuters.com",)),
    SourceProfile(
        "Agence France-Presse", "center", 0, ("afp", "agence france-presse"), ("afp.com",)
    ),
    SourceProfile("BBC", "center", 0, ("bbc", "bbc news"), ("bbc.com", "bbc.co.uk")),
    SourceProfile("Bloomberg", "center", 0, ("bloomberg", "bloomberg news"), ("bloomberg.com",)),
    SourceProfile("Financial Times", "center", 0, ("financial times", "ft"), ("ft.com",)),
    SourceProfile("The Economist", "center", 0, ("economist", "the economist"), ("economist.com",)),
    SourceProfile("USA Today", "center", 0, ("usa today",), ("usatoday.com",)),
    SourceProfile("Axios", "center", 0, ("axios",), ("axios.com",)),
    SourceProfile("The Hill", "center", 0, ("the hill",), ("thehill.com",)),
    SourceProfile("Deutsche Welle", "center", 0, ("deutsche welle", "dw"), ("dw.com",)),
    SourceProfile("France 24", "center", 0, ("france 24", "france24"), ("france24.com",)),
    SourceProfile("Sky News", "center", 0, ("sky news",), ("news.sky.com",)),
    SourceProfile("Newsweek", "center", 0, ("newsweek",), ("newsweek.com",)),
    SourceProfile("NPR", "center-left", -1, ("npr", "npr topics: news"), ("npr.org",)),
    SourceProfile("CNN", "center-left", -1, ("cnn",), ("cnn.com",)),
    SourceProfile(
        "The New York Times",
        "center-left",
        -1,
        ("new york times", "the new york times"),
        ("nytimes.com",),
    ),
    SourceProfile(
        "The Washington Post",
        "center-left",
        -1,
        ("washington post", "the washington post"),
        ("washingtonpost.com",),
    ),
    SourceProfile("Politico", "center-left", -1, ("politico",), ("politico.com",)),
    SourceProfile("Al Jazeera", "center-left", -1, ("al jazeera",), ("aljazeera.com",)),
    SourceProfile("PBS NewsHour", "center-left", -1, ("pbs", "pbs newshour"), ("pbs.org",)),
    SourceProfile("CBC News", "center-left", -1, ("cbc", "cbc news"), ("cbc.ca",)),
    SourceProfile("Time", "center-left", -1, ("time", "time magazine"), ("time.com",)),
    SourceProfile("The Guardian", "left", -2, ("guardian", "the guardian"), ("theguardian.com",)),
    SourceProfile("MSNBC", "left", -2, ("msnbc",), ("msnbc.com",)),
    SourceProfile("HuffPost", "left", -2, ("huffpost", "huffington post"), ("huffpost.com",)),
    SourceProfile("Mother Jones", "left", -2, ("mother jones",), ("motherjones.com",)),
    SourceProfile(
        "The Wall Street Journal",
        "center-right",
        1,
        ("wall street journal", "the wall street journal", "wsj"),
        ("wsj.com",),
    ),
    SourceProfile("Fox News", "center-right", 1, ("fox", "fox news"), ("foxnews.com",)),
    SourceProfile("National Review", "right", 2, ("national review",), ("nationalreview.com",)),
    SourceProfile(
        "Washington Examiner", "right", 2, ("washington examiner",), ("washingtonexaminer.com",)
    ),
    SourceProfile("New York Post", "right", 2, ("new york post", "ny post"), ("nypost.com",)),
    SourceProfile(
        "The Daily Wire", "right", 2, ("daily wire", "the daily wire"), ("dailywire.com",)
    ),
    SourceProfile("Newsmax", "right", 2, ("newsmax",), ("newsmax.com",)),
    SourceProfile("Breitbart", "right", 2, ("breitbart",), ("breitbart.com",)),
)

_BY_ALIAS = {
    re.sub(r"[^a-z0-9]+", " ", alias.casefold()).strip(): profile
    for profile in _PROFILES
    for alias in profile.aliases
}


def source_profile(publisher: str, url: str = "") -> SourceProfile | None:
    normalized = _normalized_name(publisher)
    profile = _BY_ALIAS.get(normalized)
    if profile is not None:
        return profile
    host = _hostname(url)
    for candidate in _PROFILES:
        if any(
            host == domain
            or host.endswith(f".{domain}")
            or re.sub(r"[^a-z0-9]+", " ", domain.casefold()).strip() in normalized
            for domain in candidate.domains
        ):
            return candidate
    return None


def canonical_source_name(publisher: str, url: str = "") -> str:
    profile = source_profile(publisher, url)
    if profile is not None:
        return profile.name
    cleaned = re.sub(r"\s+", " ", str(publisher or "")).strip(" -|:")
    if cleaned:
        return cleaned[:100]
    host = _hostname(url)
    return host.removeprefix("www.")[:100] or "Unknown source"


def source_bias_bucket(publisher: str, url: str = "") -> str:
    if _is_official_source(publisher, url):
        return "official"
    profile = source_profile(publisher, url)
    return profile.bias if profile is not None else "unrated"


def source_bias_score(publisher: str, url: str = "") -> int | None:
    profile = source_profile(publisher, url)
    return profile.score if profile is not None else None


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _hostname(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def _is_official_source(publisher: str, url: str) -> bool:
    host = _hostname(url)
    if host.endswith(".gov") or host.endswith(".gov.uk") or host.endswith(".gc.ca"):
        return True
    normalized = _normalized_name(publisher)
    return any(
        marker in normalized
        for marker in (
            "federal register",
            "congressional bills",
            "government",
            "ministry",
            "department of",
            "supreme court",
            "white house",
        )
    )
