# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-07

First tagged release; the monitor has run in daily production use since
early 2026 and includes the full hardening round of November 2026.

### Added
- Single-file RSS → Telegram monitor with feed groups, per-feed first-run
  baseline, and deterministic per-entry deduplication
  (`id → guid → link → content hash`)
- Hard timeout on every feed fetch (defaults to 30 s)
- Telegram delivery with up to 3 attempts, exponential backoff for transient
  failures, `Retry-After` honoring (capped at 120 s) on HTTP 429, and ~1 s
  pacing between consecutive messages
- Crash-safe state handling: state is committed immediately after each
  successful send, so already-sent entries are never re-sent
- Token-safe error handling: bot token is never written to logs or
  exception text (it is embedded in Telegram request URLs)
- UTC-correct entry timestamps via `calendar.timegm`
- State retention: `seen_posts` older than 180 days are pruned on every run;
  `feed_init` markers are never pruned by design
- Manual CLI: `--config`, `--secrets`, `--db`, `--group`, `--dry-run`

### Added (packaging)
- `pyproject.toml` (PEP 621) with a `rsswatch` console script, pinned
  dependencies, and a `test` dependency group
- Offline `pytest` battery: fetch mechanics, delivery retries, pacing,
  crash safety, dedup, retention, and timestamp handling
- README, LICENSE (MIT), and this changelog
