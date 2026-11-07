#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests


DEFAULT_DB_PATH = "rss_state.db"
SECRETS_FILENAME = "secrets.json"
USER_AGENT = "rss-watch/1.0 (+lightweight deterministic RSS monitor)"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_posts (
            group_name TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            entry_key TEXT NOT NULL,
            seen_at INTEGER NOT NULL,
            PRIMARY KEY (group_name, feed_url, entry_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feed_init (
            group_name TEXT NOT NULL,
            feed_url TEXT NOT NULL,
            initialized_at INTEGER NOT NULL,
            PRIMARY KEY (group_name, feed_url)
        )
    """)
    conn.commit()


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "telegram" in data:
        print(
            "Warning: 'telegram' section in config is ignored; "
            "credentials are read from the secrets file instead",
            file=sys.stderr,
        )

    if "groups" not in data or not isinstance(data["groups"], list):
        raise ValueError("Missing or invalid 'groups' list in config")

    for group in data["groups"]:
        if "name" not in group or "feeds" not in group:
            raise ValueError("Each group must contain 'name' and 'feeds'")
        if not isinstance(group["feeds"], list):
            raise ValueError(f"Group {group.get('name', '<unnamed>')} has invalid 'feeds'")

    return data


def load_secrets(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    telegram = data.get("telegram")
    if not isinstance(telegram, dict):
        raise ValueError(f"Missing 'telegram' section in secrets file {path}")
    if "bot_token" not in telegram or "chat_id" not in telegram:
        raise ValueError(
            f"Missing telegram.bot_token or telegram.chat_id in secrets file {path}"
        )

    return telegram


def normalize_url(url: str) -> str:
    return url.strip()


def entry_key(entry: Any) -> str:
    candidates = [
        entry.get("id"),
        entry.get("guid"),
        entry.get("link"),
        entry.get("title"),
    ]
    for c in candidates:
        if c:
            return str(c)

    raw = json.dumps(dict(entry), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def entry_time(entry: Any) -> int:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(field)
        if parsed:
            return int(time.mktime(parsed))
    return 0


def is_feed_initialized(conn: sqlite3.Connection, group_name: str, feed_url: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM feed_init WHERE group_name = ? AND feed_url = ? LIMIT 1",
        (group_name, feed_url),
    )
    return cur.fetchone() is not None


def mark_feed_initialized(conn: sqlite3.Connection, group_name: str, feed_url: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO feed_init (group_name, feed_url, initialized_at) VALUES (?, ?, ?)",
        (group_name, feed_url, int(time.time())),
    )
    conn.commit()


def has_seen(conn: sqlite3.Connection, group_name: str, feed_url: str, key: str) -> bool:
    cur = conn.execute(
        """
        SELECT 1 FROM seen_posts
        WHERE group_name = ? AND feed_url = ? AND entry_key = ?
        LIMIT 1
        """,
        (group_name, feed_url, key),
    )
    return cur.fetchone() is not None


def mark_seen(conn: sqlite3.Connection, group_name: str, feed_url: str, key: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_posts (group_name, feed_url, entry_key, seen_at)
        VALUES (?, ?, ?, ?)
        """,
        (group_name, feed_url, key, int(time.time())),
    )


def fetch_feed(feed_url: str) -> Any:
    return feedparser.parse(feed_url, agent=USER_AGENT)


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: int = 20) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=timeout,
    )
    resp.raise_for_status()


def format_message(group_name: str, feed_title: str, title: str, link: str) -> str:
    lines = [
        f"[{group_name}] New RSS post",
        f"Feed: {feed_title}",
        f"Title: {title}",
        f"Link: {link}",
    ]
    return "\n".join(lines)


def process_feed(
    conn: sqlite3.Connection,
    group_name: str,
    feed_url: str,
    bot_token: str,
    chat_id: str,
    dry_run: bool = False,
) -> Tuple[int, int]:
    parsed = fetch_feed(feed_url)

    if getattr(parsed, "bozo", 0):
        exc = getattr(parsed, "bozo_exception", None)
        print(f"Warning: feed parse issue for {feed_url}: {exc}", file=sys.stderr)

    feed_title = parsed.feed.get("title", feed_url)
    entries = list(parsed.entries)

    # Sort oldest -> newest so Telegram messages arrive in order
    entries.sort(key=entry_time)

    newly_detected = []
    initialized = is_feed_initialized(conn, group_name, feed_url)

    for entry in entries:
        key = entry_key(entry)
        if not has_seen(conn, group_name, feed_url, key):
            title = entry.get("title", "(no title)")
            link = entry.get("link", feed_url)
            newly_detected.append((key, title, link))

    if not initialized:
        # First run: remember current entries, send nothing
        for key, _, _ in newly_detected:
            mark_seen(conn, group_name, feed_url, key)
        mark_feed_initialized(conn, group_name, feed_url)
        conn.commit()
        return (len(newly_detected), 0)

    sent = 0

    for key, title, link in newly_detected:
        if dry_run:
           mark_seen(conn, group_name, feed_url, key)
           sent += 1
           continue

        msg = format_message(group_name, feed_title, title, link)
        send_telegram(bot_token, chat_id, msg)
        mark_seen(conn, group_name, feed_url, key)
        sent += 1

    conn.commit()
    return (len(newly_detected), sent)


def run(
    config_path: str,
    db_path: str,
    only_group: Optional[str],
    dry_run: bool,
    secrets_path: Optional[str] = None,
) -> int:
    config = load_config(config_path)

    if secrets_path is None:
        secrets_path = os.path.join(
            os.path.dirname(os.path.abspath(config_path)), SECRETS_FILENAME
        )
    telegram = load_secrets(secrets_path)
    bot_token = str(telegram["bot_token"]).strip()
    chat_id = str(telegram["chat_id"])

    conn = sqlite3.connect(db_path)
    init_db(conn)

    total_detected = 0
    total_sent = 0

    for group in config["groups"]:
        group_name = group["name"]
        if only_group and group_name != only_group:
            continue

        feeds = [normalize_url(u) for u in group["feeds"] if str(u).strip()]
        if not feeds:
            continue

        print(f"Processing group: {group_name}")
        for feed_url in feeds:
            try:
                detected, sent = process_feed(
                    conn=conn,
                    group_name=group_name,
                    feed_url=feed_url,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    dry_run=dry_run,
                )
                total_detected += detected
                total_sent += sent
                print(f"  {feed_url} -> detected={detected}, sent={sent}")
            except Exception as e:
                print(f"  ERROR for {feed_url}: {e}", file=sys.stderr)

    conn.close()
    print(f"Done. detected={total_detected}, sent={total_sent}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lightweight RSS -> Telegram watcher")
    parser.add_argument(
        "--config",
        default="feeds.json",
        help="Path to JSON config file (default: feeds.json)",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite state DB (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--group",
        default=None,
        help="Only process one named group",
    )
    parser.add_argument(
        "--secrets",
        default=None,
        help="Path to JSON secrets file with the Telegram credentials "
        "(default: secrets.json next to the config file)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check feeds and update state, but do not send Telegram messages",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run(
        config_path=args.config,
        db_path=args.db,
        only_group=args.group,
        dry_run=args.dry_run,
        secrets_path=args.secrets,
    )


if __name__ == "__main__":
    raise SystemExit(main())