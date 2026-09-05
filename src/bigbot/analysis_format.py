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
_COMPARISON_STOP_WORDS = {
    "a",
    "an",
    "and",
    "from",
    "he",
    "her",
    "his",
    "in",
    "on",
    "said",
    "says",
    "she",
    "the",
    "to",
    "will",
}


@dataclass(frozen=True)
class AnalysisDisplay:
    body: str
    sources: tuple[tuple[str, str], ...]


def analysis_display(value: str, *, title: str = "") -> AnalysisDisplay:
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
    formatted = _format_body(body)
    return AnalysisDisplay(
        body=_deduplicate_body(formatted, title=title),
        sources=tuple(sources[:12]),
    )


def story_update_detail(title: str, description: str, *, limit: int = 500) -> str:
    """Return the source's actual update detail without repeating its headline."""
    clean_title = plain_text(title, limit=500).strip()
    detail = plain_text(description, limit=limit).strip()
    if not detail or _comparison_text(detail) == _comparison_text(clean_title):
        return ""
    return detail


def repeats_reference(value: str, reference: str) -> bool:
    """Return whether copy restates a known headline without adding information."""
    return _is_redundant(value, (reference,))


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


def _is_redundant(value: str, references: Sequence[str]) -> bool:
    candidate = _comparison_text(value)
    if not candidate:
        return True
    candidate_words = _content_words(candidate)
    for reference in references:
        compared = _comparison_text(reference)
        if not compared:
            continue
        if candidate == compared:
            return True
        compared_words = _content_words(compared)
        shorter = min(len(candidate_words), len(compared_words))
        longer = max(len(candidate_words), len(compared_words))
        if (
            shorter >= 4
            and len(candidate_words & compared_words) / shorter >= 0.8
            and longer / shorter <= 1.45
        ):
            return True
    return False


def _content_words(value: str) -> set[str]:
    return {_word_root(word) for word in value.split() if word not in _COMPARISON_STOP_WORDS}


def _word_root(word: str) -> str:
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 5 and word.endswith("ies"):
        return f"{word[:-3]}y"
    if len(word) > 4 and word.endswith("s"):
        return word[:-1]
    return word


def _adds_no_new_detail(value: str, references: Sequence[str]) -> bool:
    candidate = _content_words(_comparison_text(value))
    known = set().union(*(_content_words(_comparison_text(item)) for item in references))
    shared = candidate & known
    novel = candidate - known
    return (
        len(candidate) >= 4
        and len(shared) >= 3
        and (len(novel) <= 2 or len(novel) / len(candidate) <= 0.18)
    )


def _deduplicate_body(value: str, *, title: str) -> str:
    """Remove analysis lines that restate the headline or an earlier section."""
    sections: list[tuple[str, list[str]]] = []
    current_title = "Summary"
    current_lines: list[str] = []
    for raw_line in value.splitlines():
        stripped = raw_line.strip()
        heading = stripped.replace("*", "").removesuffix(":").casefold()
        if heading in _SECTION_HEADINGS:
            if current_lines:
                sections.append((current_title, current_lines))
            current_title = _SECTION_HEADINGS[heading]
            current_lines = []
            continue
        if stripped:
            current_lines.append(stripped)
    if current_lines:
        sections.append((current_title, current_lines))

    references = [title] if title else []
    rendered: list[str] = []
    for heading, lines in sections:
        kept: list[str] = []
        for line in lines:
            content = re.sub(r"^[-*•]\s+", "", line).strip()
            is_repeated = _is_redundant(content, references)
            lacks_detail = heading != "Summary" and _adds_no_new_detail(content, references)
            if is_repeated or lacks_detail:
                references.append(content)
                continue
            kept.append(f"- {content}" if line.startswith(("- ", "* ", "• ")) else content)
            references.append(content)
        if not kept:
            continue
        if rendered:
            rendered.append("")
        rendered.extend((f"**{heading}**", "", *kept))
    return "\n".join(rendered).strip()


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
