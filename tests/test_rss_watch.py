"""Offline pytest battery for rss_watch.py.

Everything runs without network access: HTTP fetches and Telegram delivery
are stubbed, and state databases are created in temporary paths. No test may
sleep for real - ``time.sleep`` is patched via a fixture that records calls.
"""
import hashlib
import importlib.util
import json
import sqlite3
import sys
import time
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "rss_watch.py"
TOKEN = "123456:FAKE_TOKEN"
CHAT = "42"
UTC_EPOCH = 1709164800

ENTRIES = [
    {"id": "e1", "title": "One", "link": "http://l1"},
    {"id": "e2", "title": "Two", "link": "http://l2"},
]


class RequestException(Exception):
    """Fake stand-in for requests.RequestException."""


class FakeResponse:
    def __init__(self, code, desc=None, headers=None):
        self.status_code = code
        self.headers = headers or {}
        self._body = {"ok": False, "description": desc or ""}

    def json(self):
        return dict(self._body)


class FakeParsed:
    def __init__(self, entries):
        self.bozo = 0
        self.feed = {"title": "FeedTitle"}
        self.entries = entries


class Recorder:
    def __init__(self):
        self.sleeps = []
        self.posts = []
        self.gets = []
        self.parse_bytes = []
        self.sent = []


@pytest.fixture()
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(time, "sleep", lambda s: r.sleeps.append(s))
    return r


def load_module(rec, post_handler=None, feed_articles=None, get_handler=None):
    """Reload rss_watch.py with fake ``requests``/``feedparser`` modules."""
    req = types.ModuleType("requests")
    req.RequestException = RequestException
    req.Response = object  # referenced in type annotations at module import

    def fake_get(url, timeout=None, headers=None):
        rec.gets.append({"url": url, "timeout": timeout, "headers": headers})
        if get_handler is not None:
            return get_handler(url, timeout, headers)
        return types.SimpleNamespace(
            content=b"stub-feed", raise_for_status=lambda: None
        )

    req.get = fake_get

    def fake_post(url, data=None, timeout=None):
        rec.posts.append((url, dict(data), timeout))
        if post_handler is not None:
            return post_handler(url, data, timeout)
        return FakeResponse(200)

    req.post = fake_post

    fp = types.ModuleType("feedparser")
    fp.parse = lambda data: (
        rec.parse_bytes.append(data),
        FakeParsed(feed_articles(data) if feed_articles else []),
    )[-1]

    sys.modules["requests"] = req
    sys.modules["feedparser"] = fp

    spec = importlib.util.spec_from_file_location("rss_watch_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def open_db(tmp_path):
    return sqlite3.connect(str(tmp_path / "state.db"))


# --------------------------------------------------------------- fetching
def test_fetch_passes_bytes_with_timeout_and_user_agent(rec):
    content = b"<rss><item><id>e1</id></item></rss>"

    mod = load_module(
        rec,
        get_handler=lambda url, timeout, headers: types.SimpleNamespace(
            content=content, raise_for_status=lambda: None
        ),
        feed_articles=lambda data: [{"id": "parts"}],
    )
    parsed = mod.fetch_feed("http://example.org/feed")

    assert rec.parse_bytes[-1] == content
    call = rec.gets[-1]
    assert call["timeout"] == mod.FETCH_TIMEOUT
    assert call["headers"]["User-Agent"].startswith("rss-watch/")
    assert isinstance(parsed.entries, list)


def test_fetch_http_error_propagates(rec):
    class HTTPError(Exception):
        pass

    class RaisingResponse:
        def raise_for_status(self):
            raise HTTPError("500 boom")

    mod = load_module(
        rec,
        get_handler=lambda url, timeout, headers: RaisingResponse(),
    )
    with pytest.raises(HTTPError):
        mod.fetch_feed("http://example.org/feed")


# --------------------------------------------------------------- timestamps
def test_entry_time_treats_utc_tuple_as_utc(rec):
    mod = load_module(rec)
    utc = time.gmtime(UTC_EPOCH)
    assert mod.entry_time({"published_parsed": utc}) == UTC_EPOCH
    assert mod.entry_time({"updated_parsed": utc}) == UTC_EPOCH


def test_entry_time_defaults_and_precedence(rec):
    mod = load_module(rec)
    utc = time.gmtime(UTC_EPOCH)
    assert mod.entry_time({}) == 0
    assert (
        mod.entry_time(
            {"published_parsed": utc, "updated_parsed": time.gmtime(UTC_EPOCH + 5)}
        )
        == UTC_EPOCH
    )


# --------------------------------------------------------------- entry keys
def test_entry_key_precedence(rec):
    mod = load_module(rec)
    assert mod.entry_key({"id": "i", "guid": "g", "link": "l"}) == "i"
    assert mod.entry_key({"guid": "g", "link": "l"}) == "g"
    assert mod.entry_key({"link": "l"}) == "l"


def test_entry_key_content_hash_fallback(rec):
    mod = load_module(rec)
    a = {"title": "Same", "summary": "A"}
    b = {"title": "Same", "summary": "B"}
    ka, kb = mod.entry_key(a), mod.entry_key(b)
    assert ka != kb  # same title, different content: never hide each other
    assert ka == hashlib.sha256(
        json.dumps(a, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    assert mod.entry_key(dict(a)) == ka  # identical content: single key


def test_entry_key_id_survives_link_jitter(rec):
    mod = load_module(rec)
    assert mod.entry_key({"id": "x", "link": "http://l?utm=1"}) == "x"


# --------------------------------------------------------------- pruning
def test_prune_seen_posts(tmp_path, rec):
    mod = load_module(rec)
    conn = open_db(tmp_path)
    mod.init_db(conn)
    now = int(time.time())
    stale = now - (mod.RETENTION_DAYS + 20) * 86_400
    conn.executemany("INSERT OR IGNORE INTO seen_posts VALUES (?,?,?,?)", [
        ("g", "u1", "old-row", stale),
        ("g", "u1", "fresh-row", now),
    ])
    conn.execute("INSERT OR REPLACE INTO feed_init VALUES ('g','u1',?)", (stale,))
    conn.commit()

    assert mod.prune_seen_posts(conn) == 1
    assert mod.has_seen(conn, "g", "u1", "fresh-row")
    cur = conn.execute("SELECT COUNT(*) FROM seen_posts")
    assert cur.fetchone()[0] == 1  # the stale row is gone, the fresh one kept

    cur = conn.execute("SELECT COUNT(*) FROM feed_init WHERE feed_url = 'u1'")
    assert cur.fetchone()[0] == 1  # feed_init must never be pruned
    conn.close()


# --------------------------------------------------------------- delivery
def test_send_happy_path(rec):
    mod = load_module(rec)
    mod.send_telegram(TOKEN, CHAT, "hello")
    assert len(rec.posts) == 1
    url, payload, timeout = rec.posts[-1]
    assert payload == {
        "chat_id": CHAT,
        "text": "hello",
        "disable_web_page_preview": False,
    }
    assert timeout == mod.SEND_TIMEOUT
    assert rec.sleeps == []


def test_send_retries_429_with_retry_after(rec):
    responses = iter(
        [
            FakeResponse(429, headers={"Retry-After": "3"}),
            FakeResponse(429, headers={"Retry-After": "3"}),
            FakeResponse(200),
        ]
    )

    def handler(url, data, timeout):
        return next(responses)

    mod = load_module(rec, post_handler=handler)
    mod.send_telegram(TOKEN, CHAT, "x")
    assert rec.sleeps == [3.0, 3.0]


def test_send_429_exhaustion_raises_token_free(rec):
    mod = load_module(
        rec,
        post_handler=lambda url, data, timeout: FakeResponse(
            429, headers={"Retry-After": "5"}
        ),
    )
    with pytest.raises(mod.TelegramSendError) as exc:
        mod.send_telegram(TOKEN, CHAT, "x")
    message = str(exc.value)
    assert "429" in message
    assert TOKEN not in message
    assert len(rec.sleeps) == mod.SEND_ATTEMPTS - 1
    assert max(rec.sleeps) <= mod.MAX_RETRY_AFTER


def test_send_400_surfaces_description_without_token(rec):
    mod = load_module(
        rec,
        post_handler=lambda url, data, timeout: FakeResponse(
            400, desc="Bad Request: chat not found"
        ),
    )
    with pytest.raises(mod.TelegramSendError) as exc:
        mod.send_telegram(TOKEN, CHAT, "x")
    message = str(exc.value)
    assert "chat not found" in message
    assert TOKEN not in message


def test_send_network_error_hides_token(rec):
    def boom(url, data, timeout):
        raise RequestException(
            f"ConnectionError to https://api.telegram.org/bot{TOKEN}/sendMessage"
        )

    mod = load_module(rec, post_handler=boom)
    with pytest.raises(mod.TelegramSendError) as exc:
        mod.send_telegram(TOKEN, CHAT, "x")
    message = str(exc.value)
    assert "RequestException" in message
    assert TOKEN not in message
    assert exc.value.__cause__ is not None  # original kept for debugging


# --------------------------------------------------------------- processing
def stub_sends(mod, rec, fail_on=None):
    """Replace the module's send_telegram with a recorder."""

    def fake_send(tok, chat, text, *args, **kwargs):
        rec.sent.append((tok, chat, text))
        if fail_on is not None and len(rec.sent) == fail_on:
            raise mod.TelegramSendError("Telegram API error HTTP 500: boom")

    mod.send_telegram = fake_send


def test_process_feed_sends_only_new_and_persists(tmp_path, rec):
    mod = load_module(rec, feed_articles=lambda data: ENTRIES)
    stub_sends(mod, rec)
    conn = open_db(tmp_path)
    mod.init_db(conn)
    mod.mark_feed_initialized(conn, "g", "u1")
    conn.execute("INSERT OR IGNORE INTO seen_posts VALUES ('g','u1','e1',0)")
    conn.commit()

    detected, sent = mod.process_feed(conn, "g", "u1", TOKEN, CHAT)
    conn.close()

    assert (detected, sent) == (1, 1)
    tok, chat, text = rec.sent[-1]
    assert tok == TOKEN and chat == CHAT
    assert text.startswith("[g] New RSS post")
    assert "FeedTitle" in text
    assert rec.sleeps == []  # a single send needs no pacing

    again = open_db(tmp_path)
    assert mod.has_seen(again, "g", "u1", "e2")  # persisted across connections
    assert mod.has_seen(again, "g", "u1", "e1")
    again.close()


def test_process_feed_paces_consecutive_messages(tmp_path, rec):
    mod = load_module(rec, feed_articles=lambda data: ENTRIES)
    stub_sends(mod, rec)
    conn = open_db(tmp_path)
    mod.init_db(conn)
    mod.mark_feed_initialized(conn, "g", "u1b")
    conn.commit()

    mod.process_feed(conn, "g", "u1b", TOKEN, CHAT)
    conn.close()

    assert len(rec.sent) == 2
    assert rec.sleeps == [mod.MESSAGE_PACING]


def test_process_feed_mid_batch_failure_keeps_sent_state(tmp_path, rec):
    mod = load_module(rec, feed_articles=lambda data: ENTRIES)
    stub_sends(mod, rec, fail_on=2)
    conn = open_db(tmp_path)
    mod.init_db(conn)
    mod.mark_feed_initialized(conn, "g", "u2")
    conn.commit()

    with pytest.raises(mod.TelegramSendError):
        mod.process_feed(conn, "g", "u2", TOKEN, CHAT)
    conn.close()

    after = open_db(tmp_path)
    assert mod.has_seen(after, "g", "u2", "e1")   # sent before failure: committed
    assert not mod.has_seen(after, "g", "u2", "e2")  # failed: retriable next run
    after.close()
    assert len(rec.sent) >= 2


def test_process_feed_first_run_baselines_silently(tmp_path, rec):
    mod = load_module(rec, feed_articles=lambda data: ENTRIES)
    stub_sends(mod, rec)
    conn = open_db(tmp_path)
    mod.init_db(conn)

    detected, sent = mod.process_feed(conn, "g", "u3", TOKEN, CHAT)
    conn.close()

    assert (detected, sent) == (2, 0)
    assert rec.sent == []  # baseline must never message

    after = open_db(tmp_path)
    assert all(mod.has_seen(after, "g", "u3", k) for k in ("e1", "e2"))
    cur = after.execute(
        "SELECT 1 FROM feed_init WHERE group_name = 'g' AND feed_url = 'u3'"
    )
    assert cur.fetchone() is not None
    after.close()


def test_process_feed_dry_run_persists_without_sending(tmp_path, rec):
    mod = load_module(rec, feed_articles=lambda data: ENTRIES)
    stub_sends(mod, rec)
    conn = open_db(tmp_path)
    mod.init_db(conn)
    mod.mark_feed_initialized(conn, "g", "u4")
    conn.commit()

    detected, sent = mod.process_feed(
        conn, "g", "u4", TOKEN, CHAT, dry_run=True
    )
    conn.close()

    assert (detected, sent) == (2, 2)
    assert rec.sent == []

    after = open_db(tmp_path)
    assert all(mod.has_seen(after, "g", "u4", k) for k in ("e1", "e2"))
    after.close()
