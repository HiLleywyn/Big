# Feed and forum setup

Big turns multiple articles about one event into one Discord Forum post. Feed quality matters
more than feed quantity. A short, deliberately chosen source list produces a better forum than
dozens of overlapping feeds.

No publisher is perfectly unbiased and no feed can guarantee that every claim is true. Use a
mix of accountable reporting and direct primary sources. Big keeps every source link visible,
separates uncertain claims in the story summary, and clusters overlapping coverage into one
discussion.

## The 20 forum tags

Discord permits 20 available tags in a Forum Channel and 5 applied tags on one post. This set
uses all 20 slots:

1. `Breaking`: An explicit urgent bulletin. Big does not infer this from ordinary updates.
2. `Developing`: A known story that received significant new reporting.
3. `Politics`: Elections, legislatures, campaigns, parties, and political leadership.
4. `World`: International affairs, diplomacy, conflict, and multilateral institutions.
5. `United States`: United States national reporting and federal institutions.
6. `Markets`: Securities, bonds, yields, trading, and market moves.
7. `Economy`: Inflation, employment, growth, central banks, and interest rates.
8. `Business`: Companies, earnings, mergers, bankruptcies, and executives.
9. `Technology`: Software, hardware, semiconductors, cloud systems, and startups.
10. `AI`: Artificial intelligence models, research, products, and policy.
11. `Crypto`: Digital assets, protocols, exchanges, stablecoins, and regulation.
12. `Science`: Research, space, physics, discoveries, and scientific institutions.
13. `Health`: Public health, medicine, hospitals, drugs, vaccines, and outbreaks.
14. `Climate`: Climate science, extreme weather, emissions, and environmental events.
15. `Security`: Cyberattacks, vulnerabilities, malware, breaches, and infrastructure risk.
16. `Law`: Courts, lawsuits, prosecutions, regulation, and legal decisions.
17. `Culture`: Film, music, books, art, entertainment, and games.
18. `Sports`: Major leagues, competitions, teams, and athletes.
19. `Fact Check`: Reporting whose central purpose is verifying or debunking a specific claim.
20. `General`: A professional catch-all when no narrower category is supported by the text.

Big applies feed defaults first, then adds precise text matches. Breaking and Developing reflect
story state. It never sends a tag that does not exist in the destination Forum Channel.

## Install the tags in Discord

The fastest method is the built-in Components V2 panel:

1. Run `/news tags`.
2. Choose the Forum Channel that will receive news.
3. Review the installed and missing counts.
4. Select `Install missing tags`.

Big preserves existing tags. If the forum does not have enough free slots, remove unused tags in
Discord and run the command again. Big needs `Manage Channels` in that forum to install tags.

Manual setup also works:

1. Open the Forum Channel.
2. Select `Edit Channel`.
3. Open `Tags`.
4. Add the 20 names above with the exact spelling.
5. Save the channel.

For normal publishing, give Big these channel permissions:

* View Channel
* Create Public Threads
* Send Messages in Threads
* Embed Links
* Attach Files
* Read Message History
* Manage Threads

`Manage Channels` is only required for the automatic tag installer.

## Recommended source mix

The following feeds were checked on September 2, 2026. They returned valid RSS or Atom entries
without authentication.

| Feed | URL | Default tags | Poll |
| --- | --- | --- | --- |
| PBS News Headlines | `https://www.pbs.org/newshour/feeds/rss/headlines` | `United States` | 15 min |
| BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` | `World` | 15 min |
| Federal Reserve Press Releases | `https://www.federalreserve.gov/feeds/press_all.xml` | `Economy, Markets` | 30 min |
| SEC Press Releases | `https://www.sec.gov/news/pressreleases.rss` | `Markets, Law` | 30 min |
| ProPublica | `https://www.propublica.org/feeds/propublica/main` | `United States` | 30 min |
| Ars Technica | `https://feeds.arstechnica.com/arstechnica/index` | `Technology` | 30 min |
| NASA News Releases | `https://www.nasa.gov/news-release/feed/` | `Science` | 60 min |
| UN News | `https://news.un.org/feed/subscribe/en/news/all/rss.xml` | `World` | 30 min |

This is a balanced starting set, not an absolute truth list. PBS, BBC, ProPublica, and Ars are
editorial reporting sources. The Federal Reserve, SEC, NASA, and UN are primary institutional
sources. Institutional feeds are authoritative about what the institution said or did, but they
are not independent verification of the institution's claims.

Useful alternatives:

* NPR News: `https://feeds.npr.org/1001/rss.xml`
* BBC Business: `https://feeds.bbci.co.uk/news/business/rss.xml`
* BBC Technology: `https://feeds.bbci.co.uk/news/technology/rss.xml`
* BBC Science and Environment: `https://feeds.bbci.co.uk/news/science_and_environment/rss.xml`
* BBC Health: `https://feeds.bbci.co.uk/news/health/rss.xml`

Do not add BBC Top Stories plus every BBC category at once. Do not add NPR and PBS simply to
increase volume. Start with the table above, watch the forum for a week, then add one source only
when it fills a real coverage gap.

## Add a feed through Discord

1. Run `/news add-feed`.
2. Choose the destination Forum Channel.
3. Choose `Summaries on` or `Summaries off` for this feed.
4. Fill in the form:
   * `Feed name`: A specific internal name such as `Federal Reserve Press Releases`.
   * `Publisher`: The name readers should see, such as `Federal Reserve`.
   * `RSS or Atom URL`: Paste the exact HTTPS URL from the table.
   * `Poll minutes`: Use `15`, `30`, or `60` from the table.
   * `Forum tags`: Enter exact comma-separated tag names, such as `Economy, Markets`.
5. Submit the form.
6. Run `/news refresh` and select the feed.
7. Run `/news feeds` and check that the feed has no error.

Use `Summaries on` when the feed contains reporting that benefits from a combined factual digest.
Use `Summaries off` for feeds where the original description should be shown as written. You can
change this later in `/news feeds` by selecting the feed and using the summary setting button. The
change applies the next time an affected story is created or updated.

When several feeds report one event, Big still creates one story. If at least one source feed has
summaries enabled, Big summarizes the complete set of stored sources. This prevents an enabled
wire feed from ignoring a source just because that source feed has summaries disabled.

The first poll is limited by `BIG_MAX_BACKFILL`, which defaults to 3. Older entries are recorded
without creating a wall of old forum posts.

## Configure feeds from YAML

Use YAML when the feed list should be version controlled. Copy the example first:

```powershell
Copy-Item config.example.yaml config.yaml
```

Then replace the two Discord IDs at the top of `config.yaml`:

```yaml
guild_id: 123456789012345678
forum_channel_id: 123456789012345678
polling_interval_seconds: 900

feeds:
  - name: PBS News Headlines
    publisher: PBS News
    url: https://www.pbs.org/newshour/feeds/rss/headlines
    summarization_enabled: true
    default_tags: [United States]
    interval_seconds: 900
```

To copy a Discord ID:

1. Open Discord settings.
2. Open `Advanced` and enable `Developer Mode`.
3. Right-click the server and select `Copy Server ID`.
4. Right-click the Forum Channel and select `Copy Channel ID`.

The complete recommended feed block is already in `config.example.yaml`. It can be copied as-is
after the IDs are changed. Restart Big after editing YAML. Big synchronizes YAML feeds into SQLite
by source URL, so restarts do not duplicate them.

## Source priorities

Source priority chooses the headline and primary link when several publishers cover one story.
It is not a truth score. Direct institutional sources can receive a high priority because they
are original records of an action. Independent reporting still remains in the source list and is
used in the complete story summary.

```yaml
source_priorities:
  Federal Reserve: 100
  SEC: 100
  NASA: 100
  PBS News: 95
  BBC News: 95
  ProPublica: 92
  Ars Technica: 90
  UN News: 90
```

## Extend classification without replacing it

The built-in rules cover all 20 tags. `tag_mappings` extends those rules with terms that matter
to this server:

```yaml
tag_mappings:
  Markets: [treasury auction, wall street]
  Economy: [consumer price index, nonfarm payrolls]
  Technology: [quantum computing]
  Crypto: [digital asset]
```

Use specific event terms. Avoid broad words such as `company`, `country`, `new`, `report`, or
`update`. Broad terms create noisy tags. A story can have at most five tags, so precision matters.

## Keep the feed clean

Review these points monthly:

* Remove feeds that mainly publish opinion, sponsored posts, press-release rewrites, or repeated
  versions of the same article.
* Prefer the canonical publisher feed over a third-party RSS generator.
* Use HTTPS feeds only.
* Keep the polling interval at 15 minutes or longer unless there is a specific operational need.
* Check `/news feeds` for repeated fetch errors.
* Use `/news merge` when two threads represent the same event.
* Use `/news split` when unrelated events were clustered together.
* Keep `post_source_updates: false` so every added outlet updates the starter post without bumping
  the thread with a new reply.
* Use `retention.clear_after_days` with `archive` to keep the active forum compact without deleting
  the record.

Publishers can change feed URLs or usage terms. Recheck the publisher's RSS page before using a
feed commercially or redistributing more than headlines, summaries, and links.
