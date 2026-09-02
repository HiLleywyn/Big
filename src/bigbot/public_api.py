from __future__ import annotations

from bigbot.analysis_format import analysis_display
from bigbot.database import Database
from bigbot.domain import AnalysisState, Article, Story, utc_now
from bigbot.security import forum_title, safe_external_link


async def build_story_feed(
    database: Database, *, limit: int, public_site_url: str
) -> dict[str, object]:
    stories = await database.published_stories(limit=limit)
    items = [await _story_item(database, story, public_site_url) for story in stories]
    return {
        "version": 1,
        "generated_at": utc_now().isoformat(),
        "stories": items,
    }


async def _story_item(
    database: Database, story: Story, public_site_url: str
) -> dict[str, object]:
    articles = await database.story_articles(story.id)
    related = await database.related_stories(story.id)
    primary = _primary_article(story, articles)
    displayed_analysis = analysis_display(
        story.analysis
        if story.analysis_state is AnalysisState.READY and story.analysis
        else story.summary
    )
    discord_url = (
        f"https://discord.com/channels/{story.guild_id}/{story.discord_thread_id}"
        if story.discord_thread_id is not None
        else None
    )
    return {
        "id": story.id,
        "title": forum_title(story.title),
        "analysis": displayed_analysis.body,
        "analysis_sources": [
            {"publisher": label, "url": url} for label, url in displayed_analysis.sources
        ],
        "analysis_state": story.analysis_state.value,
        "state": story.state.value,
        "tags": list(story.tags),
        "published_at": _published_at(story, primary),
        "updated_at": story.last_updated_at.isoformat(),
        "discord_url": discord_url,
        "web_url": f"{public_site_url.rstrip('/')}/news/#story-{story.id}",
        "sources": [_source_item(article) for article in articles],
        "related": [
            {
                "id": candidate.id,
                "title": forum_title(candidate.title),
                "discord_url": (
                    f"https://discord.com/channels/{candidate.guild_id}/"
                    f"{candidate.discord_thread_id}"
                    if candidate.discord_thread_id is not None
                    else None
                ),
            }
            for candidate in related
        ],
    }


def _primary_article(story: Story, articles: list[Article]) -> Article | None:
    return next(
        (article for article in articles if article.id == story.primary_article_id),
        articles[0] if articles else None,
    )


def _published_at(story: Story, primary: Article | None) -> str:
    value = (
        (primary.published_at or primary.discovered_at)
        if primary is not None
        else (story.first_published_at or story.last_updated_at)
    )
    return value.isoformat()


def _source_item(article: Article) -> dict[str, object]:
    return {
        "publisher": article.publisher,
        "title": forum_title(article.title),
        "url": safe_external_link(article.url),
        "published_at": (
            article.published_at.isoformat() if article.published_at is not None else None
        ),
    }
