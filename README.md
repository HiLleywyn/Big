# Big

Big is a focused Discord bot that turns RSS/Atom entries and posts from public X
accounts into individual Discord forum threads. It uses a normal Discord bot account,
official Discord application commands, and X's documented API v2. It never logs in as a
Discord user.

## What it does

- Polls any public HTTPS RSS or Atom feed.
- Polls public X accounts when an official X API bearer token is configured.
- Creates one forum thread per new item, with the source, author, timestamp, summary, and
  image when available.
- Applies configured forum tags.
- Optionally replies beneath each new thread with a web-grounded OpenRouter debate brief
  whose external facts and statistics are accompanied by source links.
- Stores feeds, cursors, conditional HTTP metadata, delivery states, and an admin audit log
  in SQLite.
- Deduplicates every item across restarts.
- Fails closed after ambiguous Discord responses: the delivery becomes `uncertain` and is
  not blindly retried.
- Exposes `/feed add-rss`, `/feed add-x`, `/feed list`, `/feed poll`, `/feed pause`,
  `/feed resume`, and `/feed remove` to members with **Manage Server**.

There is no message-content intent and no prefix-command parser. Big only reads its own
slash commands and the external feeds an administrator explicitly configures.

## Discord setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications),
   add a bot, and reset/copy its token.
2. In **OAuth2 > URL Generator**, select the `bot` and `applications.commands` scopes.
3. Grant the bot **View Channels**, **Send Messages**, **Create Public Threads**,
   **Send Messages in Threads**, and **Embed Links** in the destination forum channels.
4. Enable Developer Mode in Discord if you need to copy forum tag IDs.
5. Copy `.env.example` to `.env`, set `DISCORD_TOKEN`, and optionally set
   `BIG_GUILD_ID` to your test server ID. Guild-scoped commands appear immediately;
   globally synced commands can take longer to propagate.

Big does not need Administrator permission.

## X setup

RSS works without third-party credentials. X feeds require access to the official X API
and an app-only bearer token in `X_BEARER_TOKEN`. Big resolves usernames through
`GET /2/users/by/username/:username` and reads posts through
`GET /2/users/:id/tweets`. If no token is present, only `/feed add-x` is disabled.

API access and pricing are controlled by X and can change independently of Big. A 429 is
recorded as a feed error; Big does not hammer the endpoint.

## AI briefings with OpenRouter

Set `OPENROUTER_API_KEY` to enable a second message beneath each forum starter. The default
model is `openrouter/auto`; override it with `BIG_OPENROUTER_MODEL`. Big uses OpenRouter's
current `openrouter:web_search` server tool, caps each item to two searches/eight results,
and appends the returned citation URLs to the Discord reply.

The model is instructed to provide a neutral debate brief with a summary, sourced context,
and a fair map of material competing interpretations. Feed content is explicitly treated
as untrusted input. If web search returns no URL citations, Big refuses to post the AI
reply. This is a research starting point, not a guarantee that a model interpreted every
source correctly; every reply is labeled accordingly.

Web search and model use consume OpenRouter credits per new item. Disable enrichment by
leaving `OPENROUTER_API_KEY` empty, or disable external search with
`BIG_AI_WEB_SEARCH=false` (not recommended when factual context is desired). Big sends
`provider.data_collection=deny` and defaults to zero-data-retention endpoints through
`BIG_AI_ZDR=true`.

## Run locally

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# Edit .env, then:
big doctor
big run
```

The process has no network port. It opens outbound HTTPS connections to Discord, the
configured feed origins, and (when enabled) `api.x.com`.

On first startup, Big creates `data/big.db`. Back up that file to preserve feed definitions
and deduplication history.

## Commands

```text
/feed add-rss name url forum [interval_minutes] [tag_ids]
/feed add-x name username forum [interval_minutes] [include_replies] [include_reposts] [tag_ids]
/feed list
/feed poll feed_id
/feed pause feed_id
/feed resume feed_id
/feed remove feed_id
```

`tag_ids` is a comma-separated list of up to five IDs. Add a tag when the destination
forum requires one.

On a feed's first successful poll, Big posts only the newest `BIG_MAX_BACKFILL` items so a
new subscription cannot flood a forum. Later polls rely on persistent item IDs and X's
`since_id` cursor.

## Docker

```powershell
docker build -t big .
docker run --name big --env-file .env -v big-data:/app/data big
```

Or use the hardened Compose profile (read-only root filesystem, dropped Linux capabilities,
and a persistent database volume):

```powershell
docker compose up -d --build
docker compose logs -f big
```

The image runs as an unprivileged user and stores its database in `/app/data`.

## Development

```powershell
ruff check .
ruff format --check .
mypy src
pytest --cov=bigbot --cov-report=term-missing
python -m build
```

GitHub Actions runs these checks on pushes and pull requests. Dependabot is configured for
Python packages and GitHub Actions.

## Security notes

- Secrets are read from environment variables and are never stored in SQLite.
- OpenRouter web-derived context is only posted when the response includes URL citation
  annotations; the original feed source is always listed separately.
- RSS URLs must be HTTPS, must use port 443, and must resolve entirely to public IPs.
  Redirects are rejected and response size is bounded.
- External text is reduced to plain text and Discord mentions are neutralized.
- Feed administration is server-only and requires Discord's Manage Server permission.
- Every delivery is claimed before publishing. A timeout after submission is treated as an
  unknown outcome, not permission to issue the same write again.
- Deleting a feed also deletes its delivery history. Re-adding that feed can therefore
  repost up to the configured initial backfill.

Report security issues privately through GitHub's security advisory flow rather than a
public issue.
