from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx2

from bigbot.domain import FeedItem
from bigbot.security import (
    Resolver,
    plain_text,
    resolve_host,
    safe_external_link,
    validate_feed_url,
)

URL_PATTERN = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}"


class ArticleExtractionError(RuntimeError):
    pass


def extract_article_urls(values: Iterable[str]) -> tuple[str, ...]:
    """Extract unique web links from message and embed text in display order."""
    found: list[str] = []
    seen: set[str] = set()
    for value in values:
        for match in URL_PATTERN.finditer(value or ""):
            url = match.group(0).rstrip(TRAILING_URL_PUNCTUATION)
            safe = safe_external_link(url)
            if not safe or urlsplit(safe).scheme != "https":
                continue
            key = safe.casefold()
            if key not in seen:
                seen.add(key)
                found.append(safe)
    return tuple(found)


class ArticleExtractor:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        max_bytes: int,
        resolver: Resolver = resolve_host,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._max_bytes = max_bytes
        self._resolver = resolver
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            headers={
                "Accept": "text/html, application/xhtml+xml",
                "User-Agent": "BigArticleBot/0.1 (+https://github.com/HiLleywyn/Big)",
            },
        )

    async def fetch(self, url: str) -> FeedItem:
        try:
            current = await validate_feed_url(url, resolver=self._resolver)
        except ValueError as exc:
            raise ArticleExtractionError(
                "The selected link is not a supported public HTTPS article."
            ) from exc
        response: httpx2.Response | None = None
        body = b""
        for _ in range(4):
            try:
                async with self._client.stream("GET", current) as candidate:
                    if 300 <= candidate.status_code < 400:
                        location = candidate.headers.get("location")
                        if not location:
                            raise ArticleExtractionError("The article redirect has no destination.")
                        current = await validate_feed_url(
                            urljoin(current, location), resolver=self._resolver
                        )
                        continue
                    candidate.raise_for_status()
                    content_type = candidate.headers.get("content-type", "").casefold()
                    if (
                        "text/html" not in content_type
                        and "application/xhtml+xml" not in content_type
                    ):
                        raise ArticleExtractionError("The selected link is not an HTML article.")
                    length = candidate.headers.get("content-length")
                    if length and int(length) > self._max_bytes:
                        raise ArticleExtractionError("The article is too large to process safely.")
                    chunks = bytearray()
                    async for chunk in candidate.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > self._max_bytes:
                            raise ArticleExtractionError(
                                "The article is too large to process safely."
                            )
                    body = bytes(chunks)
                    response = candidate
                    break
            except ArticleExtractionError:
                raise
            except (httpx2.HTTPError, ValueError) as exc:
                raise ArticleExtractionError(
                    f"The article could not be downloaded: {type(exc).__name__}."
                ) from exc
        if response is None:
            raise ArticleExtractionError("The article redirected too many times.")

        charset = response.encoding or "utf-8"
        try:
            html = body.decode(charset, errors="replace")
        except LookupError:
            html = body.decode("utf-8", errors="replace")
        parser = _ArticleMetadataParser()
        try:
            parser.feed(html)
            parser.close()
        except (ValueError, json.JSONDecodeError):
            pass
        metadata = parser.metadata()
        title = plain_text(metadata.get("title", ""), limit=500)
        description = plain_text(metadata.get("description", ""), limit=4000)
        if not title:
            raise ArticleExtractionError("The page does not expose an article title.")
        if not description:
            description = title

        article_url = current
        canonical_value = metadata.get("canonical", "")
        canonical = safe_external_link(urljoin(current, canonical_value)) if canonical_value else ""
        if canonical and urlsplit(canonical).scheme == "https":
            try:
                article_url = await validate_feed_url(canonical, resolver=self._resolver)
            except ValueError:
                article_url = current
        hostname = (urlsplit(article_url).hostname or "Article").removeprefix("www.")
        publisher = plain_text(metadata.get("publisher", ""), limit=200) or hostname
        identity = "manual:" + hashlib.sha256(article_url.encode("utf-8")).hexdigest()
        return FeedItem(
            external_id=identity,
            title=title,
            url=article_url,
            summary=description,
            author=None,
            published_at=_parse_date(metadata.get("published_at")),
            publisher=publisher,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class _ArticleMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._meta: dict[str, str] = {}
        self._title_parts: list[str] = []
        self._paragraphs: list[str] = []
        self._current_paragraph: list[str] | None = None
        self._in_title = False
        self._json_ld: list[str] = []
        self._current_json_ld: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = True
        elif lowered == "p":
            self._current_paragraph = []
        elif lowered == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            content = values.get("content", "").strip()
            if key and content and key not in self._meta:
                self._meta[key] = content
        elif lowered == "link" and "canonical" in values.get("rel", "").casefold():
            self._meta.setdefault("canonical", values.get("href", "").strip())
        elif lowered == "script" and "ld+json" in values.get("type", "").casefold():
            self._current_json_ld = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = False
        elif lowered == "p" and self._current_paragraph is not None:
            paragraph = plain_text(" ".join(self._current_paragraph), limit=1200)
            if len(paragraph) >= 60:
                self._paragraphs.append(paragraph)
            self._current_paragraph = None
        elif lowered == "script" and self._current_json_ld is not None:
            self._json_ld.append("".join(self._current_json_ld))
            self._current_json_ld = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._current_paragraph is not None:
            self._current_paragraph.append(data)
        if self._current_json_ld is not None:
            self._current_json_ld.append(data)

    def metadata(self) -> dict[str, str]:
        structured = _structured_metadata(self._json_ld)
        title = _first(
            self._meta.get("og:title"),
            self._meta.get("twitter:title"),
            structured.get("title"),
            " ".join(self._title_parts),
        )
        description = _first(
            self._meta.get("og:description"),
            self._meta.get("description"),
            self._meta.get("twitter:description"),
            structured.get("description"),
            self._paragraphs[0] if self._paragraphs else "",
        )
        return {
            "title": title,
            "description": description,
            "publisher": _first(self._meta.get("og:site_name"), structured.get("publisher")),
            "published_at": _first(
                self._meta.get("article:published_time"), structured.get("published_at")
            ),
            "canonical": _first(self._meta.get("canonical"), structured.get("canonical")),
        }


def _structured_metadata(blocks: Iterable[str]) -> dict[str, str]:
    for block in blocks:
        try:
            value = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_json(value):
            kind = item.get("@type")
            kinds = (
                {str(entry).casefold() for entry in kind}
                if isinstance(kind, list)
                else {str(kind).casefold()}
            )
            if not kinds.intersection(
                {"article", "newsarticle", "reportage", "analysisnewsarticle"}
            ):
                continue
            publisher = item.get("publisher")
            publisher_name = publisher.get("name", "") if isinstance(publisher, dict) else ""
            return {
                "title": str(item.get("headline") or item.get("name") or ""),
                "description": str(item.get("description") or ""),
                "publisher": str(publisher_name),
                "published_at": str(item.get("datePublished") or ""),
                "canonical": str(item.get("url") or ""),
            }
    return {}


def _walk_json(value: object) -> Iterable[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _first(*values: str | None) -> str:
    return next((value.strip() for value in values if value and value.strip()), "")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
