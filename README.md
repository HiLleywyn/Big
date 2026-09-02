# Big

Big is an open-source live news system for Discord Forum Channels.

It does not treat every article as a new post. Big normalizes and clusters reporting from
multiple publishers into one evolving story:

```text
RSS and Atom articles
        ↓
normalized facts and event signals
        ↓
one story cluster
        ↓
one Discord forum post
        ↓
sources, updates, and discussion
```

## What it does

- Polls configurable RSS and Atom feeds with conditional HTTP requests and retry backoff.
- Deduplicates tracking URLs, feed refreshes, syndicated copies, and previously seen items.
- Compares titles, descriptions, entities, keywords, numbers, event verbs, and publication
  times before attaching an article to an existing story.
- Rejects conflicting events and numerical claims to avoid merging stories that only share a
  broad topic.
- Creates one Discord forum post per story, then edits its source list as coverage arrives.
- Posts significant developments as replies in the same thread.
- Tracks `NEW`, `DEVELOPING`, `BREAKING`, `UPDATED`, `STALE`, and `MERGED` states.
- Applies only tags that currently exist on the destination forum, up to Discord's limit.
- Includes a 20-tag professional taxonomy and a Components V2 tag installer for administrators.
- Persists feeds, articles, story relationships, Discord IDs, state, delivery outcomes, and
  moderator history in SQLite.
- Fails closed when a Discord write may have succeeded but cannot be confirmed.
- Exposes health, readiness, and a read-only public story feed on port `8787`.

The deterministic clustering engine is the default and needs no paid AI service. It is behind
a small interface so a local embedding strategy can be added later without coupling feed
ingestion, persistence, or Discord publishing to it.

## Project layout

```text
src/bigbot/
  feeds/             Async RSS/Atom and optional X adapters
  normalization.py  URL, headline, entity, keyword, number, and event extraction
  clustering.py     Swappable deterministic story clustering engine
  classification.py Forum tag classification
  database.py        SQLite repositories and versioned migrations
  service.py         Ingestion, deduplication, clustering, retry, and story lifecycle
  publisher.py       Discord Forum Channel adapter
  bot.py             Discord client and /news administration commands
  health.py          Health, readiness, status, and public story HTTP endpoints
  public_api.py      Safe public story serialization for bigif.org
  config.py          Environment and YAML configuration
```

## Discord setup

1. Create an application and bot in the
   [Discord Developer Portal](https://discord.com/developers/applications).
2. Invite it with the `bot` and `applications.commands` scopes.
3. Grant it View Channels, Send Messages, Create Public Threads, Send Messages in Threads,
   Manage Threads, Embed Links, Attach Files, and Read Message History in the target forum.
4. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
5. Copy `config.example.yaml` to `config.yaml`, replace the example IDs and feed URLs, then
   start Big.

Big uses a registered bot account, does not request Message Content intent, and does not log in
as a user.

## Configuration

Secrets belong in `.env`. Feed and clustering policy belongs in `config.yaml`.

Environment variables:

```text
DISCORD_TOKEN       Required for a real Discord run
BIG_DRY_RUN         true for local dry-run, false for Discord posting
BIG_CONFIG_PATH     Path to config.yaml, default config.yaml
BIG_GUILD_ID        Optional dev guild for fast slash-command sync
BIG_DATABASE_PATH   SQLite path, default data/big.db
BIG_HEALTH_PORT     Local health port, default 8787
X_BEARER_TOKEN      Only required for official X feeds
OPENROUTER_API_KEY  Optional, enables story-level OpenRouter analysis
BIG_OPENROUTER_MODEL OpenRouter model, default deepseek/deepseek-v4-flash-0731
BIG_AI_WEB_SEARCH   Allow OpenRouter web search, default true
BIG_AI_ZDR          Require zero data retention routing, default true
BIG_RELATED_STORY_LIMIT Maximum bounded relationship candidates, default 8
```

### Story analysis

When `OPENROUTER_API_KEY` is configured, Big creates one shared OpenRouter client for the
`FeedService`. The client is closed with the service. RSS, Atom, and X all retain the same path:

```text
feed adapter -> process_item -> normalize -> deduplicate -> cluster -> persist -> analyze -> publish
```

Analysis always runs after deterministic clustering and reads every stored article in the story.
It never decides whether two reports belong to the same story. A successful result is stored on
the story and rendered in the existing Discord starter post. A failed result is recorded on the
story and the deterministic summary is published or retained.

OpenRouter receives a bounded list of recent published stories when relationship detection is
enabled. Returned IDs are rejected unless they appeared in that exact list. Accepted direct
relationships are stored once as an unordered pair, and both Discord starter posts receive a
reciprocal thread link. Shared categories, tags, names, or places are not enough for a relationship.

`BIG_AI_WEB_SEARCH=true` runs a bounded OpenRouter web research pass before the structured story
analysis. Both calls use the same shared client and remain inside the same story finalization path.
Structured JSON Schema output is required, citation links are checked against supplied articles
and OpenRouter annotations, and every returned field is validated before persistence. Keep
`BIG_AI_ZDR=true` unless you intentionally choose providers without zero data retention support.

Server administrators can use `/news settings` to validate and save a model for their server.
The saved model takes effect immediately and persists in SQLite across restarts. The environment
model remains the default for servers without an override.

```yaml
guild_id: 123456789012345678
forum_channel_id: 123456789012345678
polling_interval_seconds: 900

clustering:
  threshold: 0.68
  window_hours: 72
  stale_after_hours: 96

update_behavior:
  post_major_updates: true
  post_source_updates: false

retention:
  clear_after_days:
  action: archive
  batch_size: 25

source_priorities:
  Federal Reserve: 100
  PBS News: 95

feeds:
  - name: PBS News Headlines
    publisher: PBS News
    url: https://www.pbs.org/newshour/feeds/rss/headlines
    default_tags: [United States]
```

Each feed can override `forum_channel_id`. Default tags are combined with local automatic
classification, but the publisher resolves names against the forum's current `available_tags`
before sending anything. A required-tag forum fails safely when no configured tag exists.

The complete source-selection guide, 20-tag catalog, permission list, and copy-paste setup are in
[`docs/FEED_SETUP.md`](docs/FEED_SETUP.md).

The first successful poll is capped by `BIG_MAX_BACKFILL` so enabling a feed cannot flood a
forum. Older entries are recorded as skipped and remain deduplicated after restarts.

Retention is off until `retention.clear_after_days` is set. `archive` closes old forum threads
while keeping discussion history. `delete` removes the forum thread and should only be used when
that is the server policy. Cleanup runs in bounded batches.

## Story clustering

The default engine uses a configurable threshold and time window. Its score combines:

- normalized headline token overlap and sequence similarity
- named entity overlap
- extracted keyword overlap
- normalized description similarity
- publication-time proximity
- event compatibility, such as `cut` versus `raise`
- numerical compatibility, such as `25 bps` versus `50 bps`

A support gate requires agreement across multiple signals. A score alone cannot merge stories
with conflicting event verbs. The examples in the test suite cover paraphrased rate decisions,
same-topic but distinct events, numerical conflicts, and unrelated stories.

Canonical URL normalization removes common tracking parameters, fragments, default ports, and
query ordering differences. Database uniqueness rules add protection across feed refreshes and
process restarts.

## Commands

Members need Manage Server to change feeds or clusters.

```text
/news status
/news feeds
/news tags
/news add-feed
/news remove-feed
/news refresh
/news story
/news merge
/news split
/news reprocess
```

Feed management uses Discord Components v2 with panels, forum channel selects, buttons, and
organized forms. `add-feed` first asks for a Forum Channel, then opens a form for name, publisher,
RSS or Atom URL, polling interval, and tags. `remove-feed` opens a selection panel with a confirm
step. `refresh` lets moderators refresh one feed or all feeds from the panel. `tags` checks the
selected forum and can install the missing recommended tag names without deleting existing tags.

Published stories are available to the website at `GET /api/v1/stories?limit=50`. The response
contains only published story text, tags, source links, timestamps, and Discord thread links.
Browser access is restricted by `BIG_PUBLIC_CORS_ORIGINS`.

Story tools use short forms for IDs. `merge` moves the source cluster into the target and archives
the old Discord thread. `split` moves one article into a fresh story and forum post. `reprocess`
only retries a confirmed failed write. It refuses uncertain writes because blindly replaying one
could create duplicates.

## Run locally

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
Copy-Item config.example.yaml config.yaml
big doctor
big run
```

Health endpoints:

```text
http://127.0.0.1:8787/healthz
http://127.0.0.1:8787/readyz
http://127.0.0.1:8787/status
```

Set `BIG_DRY_RUN=true` to run ingestion, clustering, persistence, and health checks without a
Discord token or Discord writes.

## Docker

The Compose service defaults to dry-run mode, binds health endpoints to localhost, runs as a
non-root user, drops Linux capabilities, and uses a read-only root filesystem.

```powershell
docker compose -f docker-compose.yml up -d --build
docker compose -f docker-compose.yml ps
Invoke-RestMethod http://127.0.0.1:8787/readyz
```

For a real Discord run, set `BIG_DRY_RUN=false` and `DISCORD_TOKEN` in `.env`. Mount a completed
`config.yaml` at `/app/config.yaml`, or add feeds after startup with `/news add-feed`.

SQLite data is stored in the `big-data` volume at `/app/data/big.db`. Back up that file to retain
feed definitions, story history, and restart-safe deduplication.

## Development

```powershell
ruff check .
ruff format --check .
mypy src
pytest --cov=bigbot --cov-report=term-missing --cov-fail-under=80
pip-audit
python -m build
```

GitHub Actions runs the same checks and boots the container in dry-run mode before accepting the
build. Tests cover normalization, URL deduplication, story clustering, state transitions,
ambiguous Discord outcomes, bounded backfill, migrations, RSS parsing, and security controls.

## Security and reliability

- Tokens stay in environment variables and are never stored in Discord or SQLite.
- RSS URLs are restricted to public HTTPS targets with redirect and response-size controls.
- Feed text is sanitized before it reaches Discord embeds.
- Mentions are disabled for bot-authored content.
- Stable external IDs, canonical URLs, fingerprints, and database constraints prevent reposts.
- Discord rate limits are handled by discord.py. Unknown write outcomes are persisted as
  `uncertain` and require manual review.
- Structured JSON logs include feed, story, article, and clustering context needed to explain
  each routing decision.

## License

MIT
