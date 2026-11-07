# rsswatch

Lightweight, deterministic RSS monitor in a single Python file that pushes new
feed posts to a Telegram chat. Built to run unattended for months on small
boxes (e.g. an armhf Odroid) with no message loss, no duplicates, and no
hanging runs.

It reads its feed list from a plain `feeds.json`, keeps per-feed state in a
small SQLite database, and sends one Telegram message per new post grouped by
a named group.

## Features

- **Single file** (`rss_watch.py`), two dependencies: `feedparser` and `requests`
- **Feed groups** – run one group, several groups, or everything in one shot
- **Safe first run** – a new feed is silently baselined: its current entries
  are marked as seen without flooding the chat
- **Deterministic deduplication** – entries are identified by
  `id → guid → link → content hash`; two distinct entries with identical
  titles can never hide each other, and truly identical content is only
  counted once
- **Correct timestamp handling** – feed timestamps are UTC (`calendar.timegm`),
  not local time, so entry ordering does not depend on server timezone
- **Bounded network calls** – every feed fetch is time-limited (`FETCH_TIMEOUT`,
  30 s); a stalled host can never hang the whole run
- **Telegram-friendly delivery** – HTTP 429 responses honor Telegram's
  `Retry-After` (capped at 120 s), transient failures retry 3× with backoff,
  and consecutive messages are paced ~1 s apart to respect rate limits
- **Crash-safe state** – state is committed to SQLite *immediately after each
  successful send*; a kill signal between sending and committing can never
  cause duplicate notifications
- **Secrets stay out of the repo** – the bot token lives in a gitignored
  `secrets.json`; if a Telegram API error ever occurs, bot token is scrubbed
  from ALL logs and exception text
- **Bounded state** – `seen_posts` rows older than 180 days are pruned on every
  run; identity markers (`feed_init`) are deliberately never pruned
- **Offline test suite** – `pytest` battery covering fetch, delivery, pacing,
  crash safety, dedup, pruning, timestamps with zero network access

## Requirements

- Python ≥ 3.9
- Outbound network access to your feeds and `api.telegram.org`

## Installation

From a clone:

```bash
uv venv .venv && uv sync
.venv/bin/rsswatch --help
```

or with plain pip:

```bash
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/rsswatch --help
```

You can also run it straight from the checkout without installing:

```bash
python3 rss_watch.py --help
```

## Configuration

Two JSON files, both next to `--config` (or given explicitly):

**`feeds.json`** – tracked in git, contains no secrets:

```json
{
  "groups": [
    {
      "name": "daily",
      "feeds": [
        "https://example.org/feed/",
        "https://blog.example.com/rss.xml"
      ]
    }
  ]
}
```

**`secrets.json`** – gitignored, `chmod 600` recommended:

```json
{
  "telegram": {
    "bot_token": "123456:AA-your-bot-token",
    "chat_id": "123456789"
  }
}
```

Notes:

- The default secrets path is `secrets.json` in the *same directory as*
  `--config`; you can override it with `--secrets`.
- If a `telegram` section is present in `feeds.json` anyway, it is ignored
  (with a warning) — credentials are always read from the secrets file.
- Get both values from [@BotFather](https://t.me/BotFather) (`/newbot`) and an
  initial message exchange for the numeric `chat_id`.
- An empty `feeds` list is valid; the group is simply skipped.

## Usage

```text
rsswatch [--config FEEDS.JSON] [--secrets SECRETS.JSON] [--db STATE.DB]
         [--group NAME] [--dry-run]
```

| Option | Default | Meaning |
|---|---|---|
| `--config` | `feeds.json` | Path to the feed list |
| `--secrets` | `secrets.json` next to `--config` | Path to the credentials file |
| `--db` | `rss_state.db` | SQLite state database |
| `--group` | all groups | Only process one named group |
| `--dry-run` | off | Fetch feeds and **update state, but send nothing** |

Typical invocations:

```bash
rsswatch                           # everything
rsswatch --group daily             # one group
rsswatch --dry-run                 # state sync without messages
```

**A word on `--dry-run`:** it marks currently unseen entries as seen, exactly
like a send-only run would after delivery. Treat it as a "catch up silently"
mode, not as a preview of what would be sent.

There is no scheduler inside; run it as often as you like, e.g. from cron:

```cron
42 */2 * * *  cd /path/to/rsswatch && .venv/bin/rsswatch >> rss_watch.log 2>&1
```

## How it works

1. Load `feeds.json` groups and credentials from the secrets file.
2. For every feed: fetch over HTTP with a hard timeout, parse with feedparser.
3. Sort entries oldest → newest by publication time (UTC), so messages arrive
   in feed order.
4. Detect entries whose key (`id → guid → link → hash of content`) was never
   recorded for this `(group, feed)` pair.
5. If the feed was never seen before, just record all current entries
   (no messages) and mark the feed initialized.
6. Otherwise send one message per new entry:

   ```text
   [daily] New RSS post
   Feed: Example Blog
   Title: Something happened
   Link: https://example.org/post/1
   ```

7. Commit the sent entry to SQLite immediately after each successful send.
8. Optionally prune `seen_posts` older than `RETENTION_DAYS` (180).

### State database

SQLite, two tables:

- `seen_posts(group_name, feed_url, entry_key, seen_at)` – what was already
  notified, pruned after 180 days
- `feed_init(group_name, feed_url, initialized_at)` – first-run markers,
  never pruned (losing one would re-baseline a feed and swallow its posts)

### Reliability tuning

All knobs live as constants at the top of `rss_watch.py`:

| Constant | Default | Meaning |
|---|---|---|
| `FETCH_TIMEOUT` | 30 s | HTTP request window for feed fetches |
| `SEND_TIMEOUT` | 20 s | HTTP request window for Telegram calls |
| `SEND_ATTEMPTS` | 3 | Attempts per message |
| `RETRY_BASE_DELAY` | 2 s | Backoff base for transient failures |
| `MAX_RETRY_AFTER` | 120 s | Cap for Telegram's `Retry-After` |
| `MESSAGE_PACING` | 1 s | Delay between consecutive messages |
| `RETENTION_DAYS` | 180 | `seen_posts` retention |

## Security notes

- The bot token is the only credential; keep it in `secrets.json` (gitignored)
  and restrict permissions (`chmod 600`).
- Network error messages from `api.telegram.org` can embed the bot token (it
  is part of the request URL); rsswatch never prints them. Delivery failures
  are re-raised as a token-free `TelegramSendError`, and any unexpected
  exception text is scrubbed before it can reach logs.
- If a token ever does leak (a copy of the folder, a pushed repo), revoke it in
  [@BotFather](https://t.me/BotFather) with `/revoke` and rotate.

## Development

```bash
uv sync --group test     # or: pip install pytest
uv run --group test pytest
```

The test suite is fully offline: network and Telegram calls are stubbed, state
is created in temporary files. Layout:

```text
rss_watch.py     the whole application
feeds.json       feed groups (no secrets)
secrets.json     gitignored credentials
rss_state.db     local state (gitignored)
tests/           offline pytest battery
```

## Versioning

Static version in `pyproject.toml` following SemVer; every release is also
tagged in git (`v1.0.0`). Bump the version, commit, tag:

```bash
$EDITOR pyproject.toml            # version = "x.y.z"
git add pyproject.toml CHANGELOG.md
git commit -m "Release x.y.z"
git tag -a "vx.y.z" -m "rsswatch x.y.z"
```

## License

[MIT](LICENSE) © 2026 Radek Svítil
