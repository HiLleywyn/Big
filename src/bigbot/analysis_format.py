from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from bigbot.domain import Article, StoryUpdate
from bigbot.normalization import normalize_url
from bigbot.security import plain_text, safe_external_link

_MARKDOWN_LINK = re.compile(r"^-\s*\[([^\]]+)]\((https?://[^)\s]+)\)\s*$")
_SECTION_HEADINGS = {
    "summary": "Summary",
    "key facts": "Key facts",
    "useful context": "Context",
    "context": "Context",
    "unclear or disputed": "Unclear or disputed",
}


@dataclass(frozen=True)
class AnalysisDisplay:
    body: str
    sources: tuple[tuple[str, str], ...]


def analysis_display(value: str) -> AnalysisDisplay:
    """Separate verified citations from the user-facing analysis sections."""
    body: list[str] = []
    sources: list[tuple[str, str]] = []
    in_sources = False
    for raw_line in value.splitlines():
        heading = raw_line.strip().replace("*", "").removesuffix(":").casefold()
        if heading in {"analysis sources", "sources"}:
            in_sources = True
            continue
        if not in_sources:
            body.append(raw_line.rstrip())
            continue
        match = _MARKDOWN_LINK.fullmatch(raw_line.strip())
        if match is None:
            continue
        label = plain_text(match.group(1), limit=100)
        url = safe_external_link(match.group(2))
        if label and url and (label, url) not in sources:
            sources.append((label, url))
    return AnalysisDisplay(body=_format_body(body), sources=tuple(sources[:12]))


def story_update_detail(title: str, description: str, *, limit: int = 500) -> str:
    """Return the source's actual update detail without repeating its headline."""
    clean_title = plain_text(title, limit=500).strip()
    detail = plain_text(description, limit=limit).strip()
    if not detail or _comparison_text(detail) == _comparison_text(clean_title):
        return ""
    return detail


def visible_story_updates(
    primary: Article | None, updates: Sequence[StoryUpdate]
) -> tuple[StoryUpdate, ...]:
    """Hide repeated transport copies while retaining genuinely new reporting."""
    seen = {_article_signature(primary)} if primary is not None else set()
    seen_urls = {normalize_url(primary.canonical_url)} if primary is not None else set()
    visible: list[StoryUpdate] = []
    for update in updates:
        signature = _article_signature(update.article)
        canonical_url = normalize_url(update.article.canonical_url)
        if signature in seen or (canonical_url and canonical_url in seen_urls):
            continue
        seen.add(signature)
        if canonical_url:
            seen_urls.add(canonical_url)
        if not update.detail and not story_update_detail(
            update.article.title, update.article.description
        ):
            continue
        visible.append(update)
    return tuple(visible)


def _article_signature(article: Article) -> tuple[str, str, str]:
    return (
        _comparison_text(article.publisher),
        _comparison_text(article.title),
        _comparison_text(article.description),
    )


def _comparison_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _format_body(lines: list[str]) -> str:
    formatted: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        heading = stripped.replace("*", "").removesuffix(":").casefold()
        if heading in _SECTION_HEADINGS:
            if formatted and formatted[-1] != "":
                formatted.append("")
            formatted.extend((f"**{_SECTION_HEADINGS[heading]}**", ""))
        elif not stripped:
            if formatted and formatted[-1] != "":
                formatted.append("")
        else:
            formatted.append(raw_line.rstrip())
    return "\n".join(formatted).strip()
