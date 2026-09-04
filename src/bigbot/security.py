from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    pass


Resolver = Callable[[str, int], Awaitable[set[ipaddress.IPv4Address | ipaddress.IPv6Address]]]


async def resolve_host(
    hostname: str, port: int
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    def lookup() -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return {ipaddress.ip_address(record[4][0]) for record in records}

    try:
        return await asyncio.to_thread(lookup)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"feed hostname could not be resolved: {hostname}") from exc


async def validate_feed_url(url: str, *, resolver: Resolver = resolve_host) -> str:
    if len(url) > 2048:
        raise UnsafeUrlError("feed URL is too long")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UnsafeUrlError("feed URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeUrlError("feed URL has an invalid authority")
    if parsed.port not in {None, 443}:
        raise UnsafeUrlError("feed URL must use the default HTTPS port")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeUrlError("feed hostname is invalid") from exc
    addresses = await resolver(hostname, 443)
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeUrlError("feed hostname resolves to a non-public address")
    return url


def safe_external_link(url: str) -> str:
    if len(url) > 2048:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return url


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str, *, limit: int) -> str:
    parser = _TextExtractor()
    escaped = re.sub(
        r"&(?!(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]+);)",
        "&amp;",
        value,
        flags=re.IGNORECASE,
    )
    parser.feed(escaped)
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    text = neutralize_mentions(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def neutralize_mentions(value: str) -> str:
    return re.sub(
        r"@(everyone|here|[!&]?\d+)",
        lambda match: f"@\u200b{match.group(1)}",
        value,
        flags=re.IGNORECASE,
    )


def forum_title(value: str) -> str:
    with_breaks = re.sub(r"(?i)<br\s*/?>", "\n", value)
    lines = [line.strip() for line in with_breaks.splitlines() if line.strip()]
    first = lines[0] if lines else value
    has_bullet_continuation = any(re.match(r"^\s*(?:[-*•]|\d+[.)])\s+", line) for line in lines[1:])
    if has_bullet_continuation and " - " in first:
        first = first.split(" - ", 1)[0]
    first = re.sub(r"^\s*(?:#{1,6}|[-*•]|\d+[.)])\s+", "", first)
    first = re.sub(
        r"\s+(?:-|\|)\s+(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}\s*$",
        "",
        first,
        flags=re.IGNORECASE,
    )
    title = plain_text(first, limit=100).strip(" .-|•")
    return title or "New feed item"


def publisher_label(value: str, url: str) -> str:
    label = plain_text(value, limit=100).strip(" \"'")
    google_query = re.search(r"(?i)site:([a-z0-9.-]+)", label)
    if google_query:
        label = google_query.group(1)
    host = re.sub(r"^www\.", "", urlparse(url).hostname or "", flags=re.IGNORECASE)
    candidate = label.casefold().removeprefix("www.")
    known = {
        "reuters.com": "Reuters",
        "apnews.com": "AP",
        "bbc.com": "BBC News",
        "bbc.co.uk": "BBC News",
        "cnn.com": "CNN",
        "npr.org": "NPR",
    }
    if candidate in known:
        return known[candidate]
    if "google news" in candidate and host == "news.google.com":
        return "Google News"
    return label or known.get(host, host or "Source")
