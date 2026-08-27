from __future__ import annotations

import argparse
import asyncio
import json

from bigbot.bot import BigBot
from bigbot.config import ConfigurationError, load_settings
from bigbot.database import Database
from bigbot.logging_config import configure_logging


async def _doctor() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_path)
    await database.connect()
    feeds = await database.list_feeds()
    await database.close()
    print(
        json.dumps(
            {
                "status": "ok",
                "database": str(settings.database_path),
                "feeds": len(feeds),
                "discord_token": "configured" if settings.discord_token else "missing",
                "x": "enabled" if settings.x_bearer_token else "disabled",
                "ai": "enabled" if settings.openrouter_api_key else "disabled",
                "ai_model": settings.openrouter_model,
                "ai_web_search": settings.ai_web_search,
                "guild_sync": settings.guild_id or "global",
            },
            indent=2,
        )
    )


def _run() -> None:
    settings = load_settings(require_discord=True)
    configure_logging(settings.log_level)
    bot = BigBot(settings)
    bot.run(settings.discord_token or "", log_handler=None)


def main() -> None:
    parser = argparse.ArgumentParser(prog="big", description="Big Discord forum feed bot")
    parser.add_argument("command", nargs="?", choices=("run", "doctor"), default="run")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            asyncio.run(_doctor())
        else:
            _run()
    except ConfigurationError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
