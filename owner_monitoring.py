"""Private owner notifications with durable cursors and conservative delivery recovery."""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from telegram.error import BadRequest, Forbidden, RetryAfter

logger = logging.getLogger(__name__)


def recipient_id(raw, owners):
    try:
        candidate = int(raw) if raw else (next(iter(owners)) if len(owners) == 1 else None)
    except (ValueError, TypeError):
        return None
    return candidate if candidate in owners and candidate > 0 else None


def fetch_events(config, user_id, states):
    url = urlsplit(config.url)
    endpoint = urlunsplit((url.scheme, url.netloc, "/internal/owner-ai/v1/monitoring", "", ""))
    payload = json.dumps({"version": "1", "user_id": user_id, "states": states}).encode()
    if len(payload) > 16_384:
        raise ValueError("Monitoring cursor exceeds limit")
    request = urllib.request.Request(endpoint, data=payload, method="POST", headers={
        "Authorization": f"Bearer {config.token}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
        raw = response.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("Monitoring response exceeds limit")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("version") != "1":
        raise ValueError("Invalid monitoring response")
    if not isinstance(data.get("states"), dict) or len(data["states"]) > 50:
        raise ValueError("Invalid monitoring states")
    if not isinstance(data.get("events"), list) or len(data["events"]) > 100:
        raise ValueError("Invalid monitoring events")
    for event in data["events"]:
        if (not isinstance(event, dict) or set(event) != {"point_code", "kind", "problem", "occurred_at", "text"}
            or event["point_code"] not in data["states"]
            or event["kind"] not in ("new", "repeat", "recovery")
            or event["problem"] not in ("source_unavailable", "offline", "no_sales")
            or not isinstance(event["text"], str) or not 0 < len(event["text"]) <= 3500
            or not isinstance(event["occurred_at"], str)):
            raise ValueError("Invalid monitoring event")
    return data


class MonitorStore:
    def __init__(self, path, user_id):
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("Monitoring recipient must be a private Telegram user")
        self.path, self.user_id = Path(path), user_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cursors (
                    user_id INTEGER PRIMARY KEY, version INTEGER NOT NULL,
                    states TEXT NOT NULL, polled_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, message_id INTEGER, retry_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS cards (
                    user_id INTEGER NOT NULL, point_code TEXT NOT NULL, problem TEXT NOT NULL,
                    message_id INTEGER NOT NULL, PRIMARY KEY(user_id,point_code,problem)
                );
            """)
            # Telegram has no idempotency key for sendMessage. An interrupted
            # attempt may have delivered; never turn it into a blind resend.
            db.execute("UPDATE deliveries SET status='uncertain' WHERE user_id=? AND status='attempting'", (user_id,))
            db.execute("INSERT OR IGNORE INTO cursors VALUES (?,0,'{}',0)", (user_id,))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def cursor(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM cursors WHERE user_id=?", (self.user_id,)).fetchone()
            return row["version"], json.loads(row["states"])

    def record(self, version, data):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE cursors SET version=version+1,states=?,polled_at=? WHERE user_id=? AND version=?",
                (json.dumps(data["states"]), time.time(), self.user_id, version),
            ).rowcount
            if changed != 1:
                raise ValueError("Monitoring cursor changed concurrently")
            for event in data["events"]:
                identity = [self.user_id, event["point_code"], event["problem"], event["kind"], event["occurred_at"]]
                event_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(event_id,user_id,payload,status) VALUES (?,?,?,'ready')",
                    (event_id, self.user_id, json.dumps(event)),
                )

    def next_delivery(self):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM deliveries WHERE user_id=? AND status='ready' AND retry_at<=? ORDER BY seq LIMIT 1",
                (self.user_id, time.time()),
            ).fetchone()
            return {**dict(row), "event": json.loads(row["payload"])} if row else None

    def card(self, event):
        with self.connection() as db:
            row = db.execute("SELECT message_id FROM cards WHERE user_id=? AND point_code=? AND problem=?",
                             (self.user_id, event["point_code"], event["problem"])).fetchone()
            return row[0] if row else None

    def uncertain_problem(self, event):
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM deliveries WHERE user_id=? AND status='uncertain'", (self.user_id,))
            return any((item := json.loads(row[0]))["point_code"] == event["point_code"]
                       and item["problem"] == event["problem"] for row in rows)

    def status(self, seq, status, *, retry_at=0, message_id=None):
        with self.connection() as db:
            changed = db.execute(
                "UPDATE deliveries SET status=?,retry_at=?,message_id=COALESCE(?,message_id) WHERE seq=? AND user_id=?",
                (status, retry_at, message_id, seq, self.user_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Unknown monitoring delivery")

    def claim(self, seq):
        with self.connection() as db:
            return db.execute(
                "UPDATE deliveries SET status='attempting' WHERE seq=? AND user_id=? AND status='ready'",
                (seq, self.user_id),
            ).rowcount == 1

    def delivered(self, delivery, message_id):
        event = delivery["event"]
        with self.connection() as db:
            db.execute("UPDATE deliveries SET status='sent',message_id=? WHERE seq=? AND user_id=?",
                       (message_id, delivery["seq"], self.user_id))
            if event["kind"] == "recovery":
                db.execute("DELETE FROM cards WHERE user_id=? AND point_code=? AND problem=?",
                           (self.user_id, event["point_code"], event["problem"]))
            else:
                db.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?)",
                           (self.user_id, event["point_code"], event["problem"], message_id))


async def deliver_pending(bot, store):
    while delivery := store.next_delivery():
        event = delivery["event"]
        _, states = store.cursor()
        current_problem = states.get(event["point_code"], {}).get("primary_problem")
        if event["kind"] != "recovery" and current_problem != event["problem"]:
            store.status(delivery["seq"], "superseded")
            continue
        if event["kind"] == "recovery" and store.card(event) is None and not store.uncertain_problem(event):
            store.status(delivery["seq"], "superseded")
            continue
        card_id = store.card(event) if event["kind"] == "repeat" else None
        if event["kind"] == "repeat" and card_id is None and store.uncertain_problem(event):
            store.status(delivery["seq"], "uncertain")
            continue
        if not store.claim(delivery["seq"]):
            continue
        try:
            if card_id:
                await bot.edit_message_text(chat_id=store.user_id, message_id=card_id, text=event["text"])
                message_id = card_id
            else:
                message = await bot.send_message(chat_id=store.user_id, text=event["text"])
                message_id = message.message_id
            store.delivered(delivery, message_id)
        except RetryAfter as exc:
            delay = exc.retry_after
            delay = delay.total_seconds() if hasattr(delay, "total_seconds") else delay
            store.status(delivery["seq"], "ready", retry_at=time.time() + float(delay) + 1)
            return
        except BadRequest as exc:
            if card_id and "message is not modified" in str(exc).lower():
                store.delivered(delivery, card_id)
            else:
                store.status(delivery["seq"], "failed")
                logger.warning("owner_monitoring_delivery_rejected")
        except Forbidden:
            store.status(delivery["seq"], "failed")
            logger.warning("owner_monitoring_recipient_unavailable")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.status(delivery["seq"], "uncertain")
            logger.warning("owner_monitoring_delivery_uncertain error_type=%s", type(exc).__name__)


async def monitoring_loop(application, config, store):
    while True:
        try:
            version, states = store.cursor()
            data = await asyncio.to_thread(fetch_events, config, store.user_id, states)
            store.record(version, data)
            await deliver_pending(application.bot, store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("owner_monitoring_poll_failed error_type=%s", type(exc).__name__)
        await asyncio.sleep(300)
