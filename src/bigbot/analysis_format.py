from __future__ import annotations

import re
from dataclasses import dataclass

from bigbot.security import plain_text, safe_external_link

_MARKDOWN_LINK = re.compile(r"^-\s*\[([^\]]+)]\((https?://[^)\s]+)\)\s*$")


@dataclass(frozen=True)
class AnalysisDisplay:
    body: str
    sources: tuple[tuple[str, str], ...]


def analysis_display(value: str) -> AnalysisDisplay:
    """Separate verified citations from the three user-facing analysis sections."""
    body: list[str] = []
    sources: list[tuple[str, str]] = []
    in_sources = False
    for raw_line in value.splitlines():
        heading = raw_line.strip().replace("*", "").removesuffix(":").casefold()
        if heading == "analysis sources":
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
    return AnalysisDisplay(body="\n".join(body).strip(), sources=tuple(sources[:12]))
