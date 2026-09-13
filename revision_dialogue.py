"""Durable, isolated revision drafts and a journal for resumable writes.

SQLite stores workflow state, never replaces Google Sheets as accounting truth.
Network clients and asyncio tasks live outside this store.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path


class RevisionConflict(RuntimeError):
    pass


class RevisionInputError(ValueError):
    pass


def is_revision_request(text):
    """Only selects an entry point; the model interprets the actual message."""
    normalized = str(text or "").casefold()
    return bool(
        re.search(r"\bревизи\w*", normalized)
        or re.search(r"(?:запиш|запис|внес|заполн|сохран)\w*.*\bостат\w*", normalized)
        or re.search(r"\bостат\w*.*(?:запиш|запис|внес|сохран)", normalized)
    )


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:[.,]\d{1,4})?", value):
        raise RevisionInputError("Количество должно быть неотрицательным числом.")
    result = Decimal(value.replace(",", "."))
    if not result.is_finite() or result > 1_000_000:
        raise RevisionInputError("Количество вне допустимого диапазона.")
    return format(result.normalize(), "f")


def validate_proposal(proposal, locations, items):
    if not isinstance(proposal, dict) or set(proposal) != {"action", "records", "clarification"}:
        raise RevisionInputError("Не удалось проверить ответ модели.")
    if proposal["action"] not in ("update", "save", "show", "cancel", "other"):
        raise RevisionInputError("Не удалось определить действие.")
    if not isinstance(proposal["clarification"], str) or len(proposal["clarification"]) > 400:
        raise RevisionInputError("Слишком длинное уточнение.")
    records = proposal["records"]
    if not isinstance(records, list) or len(records) > 2:
        raise RevisionInputError("Пришлите не больше двух точек за сообщение.")
    clean = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "location",
            "previous_location",
            "date",
            "values",
        }:
            raise RevisionInputError("Не удалось проверить состав ревизии.")
        location = record["location"]
        if not isinstance(location, str) or location not in ("", *locations):
            raise RevisionInputError("Неизвестная локация.")
        previous = record["previous_location"]
        if not isinstance(previous, str) or previous not in ("", *locations):
            raise RevisionInputError("Неизвестная прежняя локация.")
        day = record["date"]
        if not isinstance(day, str) or (day and date.fromisoformat(day).isoformat() != day):
            raise RevisionInputError("Не удалось проверить дату.")
        values = record["values"]
        if not isinstance(values, dict) or not set(values).issubset(items):
            raise RevisionInputError("Неизвестный товар.")
        clean.append(
            {
                "location": location,
                "previous_location": previous,
                "date": day,
                "values": {name: number(value) for name, value in values.items()},
            }
        )
    if proposal["action"] in ("show", "cancel", "other") and clean:
        raise RevisionInputError("Это действие не должно менять остатки.")
    return {**proposal, "records": clean}


def apply_proposal(draft, proposal, message_date):
    """Return a new draft. Missing values never overwrite previous quantities."""
    updated = json.loads(json.dumps(draft))
    action = proposal["action"]
    if action == "cancel":
        updated.update(status="cancelled", preview=None)
        return updated
    if action in ("other", "show"):
        return updated
    records = {row["location"]: row for row in updated["records"]}
    pending = updated.get("unassigned", [])
    patches = list(proposal["records"])
    explicit = {row["location"] for row in patches if row["location"]}
    if pending and len(explicit) == 1:
        target = next(iter(explicit))
        patches = [{**row, "location": target} for row in pending] + patches
        pending = []
    elif pending:
        # Preserve evidence awaiting a location; do not silently discard it.
        pending = list(pending)
    clarification = proposal["clarification"]
    for patch in patches:
        location = patch["location"]
        previous = patch["previous_location"]
        if previous:
            if previous not in records or not location or location in records:
                raise RevisionInputError("Перенос точки неоднозначен; уточните локацию.")
            records[location] = {**records.pop(previous), "location": location}
        if not location and len(records) == 1:
            location = next(iter(records))
        if not location:
            if patch["values"] or patch["date"]:
                pending.append(patch)
            clarification = "На какой точке считаем? Можно указать также Дома или Гараж."
            continue
        current = records.setdefault(
            location,
            {
                "location": location,
                "date": patch["date"] or message_date,
                "values": {},
            },
        )
        if patch["date"]:
            current["date"] = patch["date"]
        current["values"].update(patch["values"])
    if len(pending) > 8:
        raise RevisionInputError("Сначала укажите точку для уже присланных остатков.")
    updated.update(
        records=list(records.values()),
        unassigned=pending,
        clarification=clarification,
        preview=None,
        status="draft",
        save_requested=action == "save",
        needs_review=bool(
            updated.get("needs_review")
            or clarification
            or pending
            or len(patches) > 1
            or any(row["previous_location"] for row in patches)
        ),
    )
    return updated


class RevisionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS drafts (
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    version INTEGER NOT NULL, generation INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(chat_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL, text TEXT NOT NULL,
                    message_date TEXT NOT NULL, state TEXT NOT NULL,
                    proposal TEXT, PRIMARY KEY(chat_id,user_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    version INTEGER NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            # An interrupted provider call is never retried automatically: it may
            # have been charged. Parsed responses can be applied without another call.
            interrupted = db.execute(
                "SELECT DISTINCT chat_id,user_id FROM messages WHERE state='processing'",
            ).fetchall()
            db.execute("UPDATE messages SET state='interrupted' WHERE state='processing'")
            for key in interrupted:
                row = db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?", tuple(key)
                ).fetchone()
                if row:
                    payload = json.loads(row["payload"])
                    payload["needs_review"] = True
                    db.execute(
                        "UPDATE drafts SET payload=? WHERE chat_id=? AND user_id=?",
                        (json.dumps(payload), *key),
                    )

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

    @staticmethod
    def _load(row):
        if row is None:
            return None
        return {
            **json.loads(row["payload"]),
            "version": row["version"],
            "generation": row["generation"],
        }

    def get(self, chat_id, user_id):
        with self.connection() as db:
            return self._load(
                db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                ).fetchone()
            )

    def accept(self, chat_id, user_id, message_id, text, message_date):
        if (
            chat_id != user_id
            or user_id <= 0
            or not isinstance(text, str)
            or not 0 < len(text) <= 1200
        ):
            raise RevisionInputError("Ревизия доступна в личном чате; сообщение до 1200 символов.")
        date.fromisoformat(message_date)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM messages WHERE chat_id=? AND user_id=? AND message_id=?",
                (chat_id, user_id, message_id),
            ).fetchone():
                return False
            active = db.execute(
                "SELECT count(*) FROM messages WHERE state IN ('pending','processing','parsed')",
            ).fetchone()[0]
            if active >= 20:
                raise RevisionInputError(
                    "Очередь заполнена. Дождитесь ответа и повторите сообщение."
                )
            row = db.execute(
                "SELECT * FROM drafts WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            draft = self._load(row)
            if draft and draft["status"] in ("saving", "uncertain"):
                raise RevisionConflict("Сначала нужно завершить сверку начатой записи.")
            if draft is None or draft["status"] in ("cancelled", "saved"):
                draft = {
                    "id": uuid.uuid4().hex,
                    "status": "draft",
                    "records": [],
                    "unassigned": [],
                    "clarification": "",
                    "preview": None,
                    "save_requested": False,
                }
                db.execute(
                    "INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?)",
                    (
                        chat_id,
                        user_id,
                        (row["version"] + 1) if row else 0,
                        (row["generation"] + 1) if row else 1,
                        json.dumps(draft),
                    ),
                )
            else:
                db.execute(
                    "UPDATE drafts SET generation=generation+1 WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                )
            db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,'pending',NULL)",
                (chat_id, user_id, message_id, text, message_date),
            )
        return True

    def next_message(self, chat_id, user_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE chat_id=? AND user_id=? "
                "AND state IN ('pending','parsed') ORDER BY message_id LIMIT 1",
                (chat_id, user_id),
            ).fetchone()
            return dict(row) if row else None

    def has_message(self, chat_id, user_id, message_id):
        with self.connection() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM messages WHERE chat_id=? AND user_id=? AND message_id=?",
                    (chat_id, user_id, message_id),
                ).fetchone()
            )

    @staticmethod
    def _assert_no_active_messages(db, chat_id, user_id):
        if db.execute(
            "SELECT 1 FROM messages WHERE chat_id=? AND user_id=? "
            "AND state IN ('pending','processing','parsed') LIMIT 1",
            (chat_id, user_id),
        ).fetchone():
            raise RevisionConflict("Сначала нужно разобрать все уже принятые сообщения.")

    def resume_keys(self):
        with self.connection() as db:
            return [
                tuple(row)
                for row in db.execute(
                    "SELECT DISTINCT chat_id,user_id FROM messages "
                    "WHERE state IN ('pending','parsed')",
                ).fetchall()
            ]

    def cancel(self, chat_id, user_id, *, version, generation):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(
                db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                ).fetchone()
            )
            if current is None or (current["version"], current["generation"]) != (
                version,
                generation,
            ):
                raise RevisionConflict("Кнопка устарела; перечитайте текущий черновик.")
            if current["status"] in ("saving", "uncertain"):
                raise RevisionConflict("Начатую запись сначала нужно сверить.")
            current.update(status="cancelled", preview=None)
            db.execute(
                "UPDATE drafts SET version=version+1,generation=generation+1,payload=? "
                "WHERE chat_id=? AND user_id=?",
                (json.dumps(current), chat_id, user_id),
            )
            db.execute(
                "UPDATE messages SET state='cancelled' WHERE chat_id=? AND user_id=? "
                "AND state IN ('pending','parsed','processing')",
                (chat_id, user_id),
            )

    def mark_message(self, chat_id, user_id, message_id, state, proposal=None):
        if state not in ("processing", "parsed", "failed"):
            raise ValueError("Invalid message state")
        with self.connection() as db:
            db.execute(
                "UPDATE messages SET state=?,proposal=? WHERE chat_id=? AND user_id=? "
                "AND message_id=?",
                (
                    state,
                    json.dumps(proposal) if proposal is not None else None,
                    chat_id,
                    user_id,
                    message_id,
                ),
            )
            if state == "failed":
                row = db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?", (chat_id, user_id)
                ).fetchone()
                if row:
                    payload = json.loads(row["payload"])
                    payload["needs_review"] = True
                    db.execute(
                        "UPDATE drafts SET payload=? WHERE chat_id=? AND user_id=?",
                        (json.dumps(payload), chat_id, user_id),
                    )

    def apply_message(self, chat_id, user_id, message_id, proposal, *, expected_version):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute(
                "SELECT * FROM messages WHERE chat_id=? AND user_id=? AND message_id=?",
                (chat_id, user_id, message_id),
            ).fetchone()
            current = self._load(
                db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                ).fetchone()
            )
            if event is None or current is None:
                raise RevisionConflict("Сообщение или черновик не найден.")
            if event["state"] == "done":
                return current
            if current["version"] != expected_version:
                raise RevisionConflict("Черновик изменился; старый ответ модели не применён.")
            if current["status"] not in ("draft", "preview"):
                raise RevisionConflict("Этот черновик уже завершён.")
            result = apply_proposal(current, proposal, event["message_date"])
            result["version"] += 1
            db.execute(
                "UPDATE drafts SET version=?,payload=? WHERE chat_id=? AND user_id=?",
                (result["version"], json.dumps(result), chat_id, user_id),
            )
            db.execute(
                "UPDATE messages SET state='done',proposal=? WHERE chat_id=? AND user_id=? "
                "AND message_id=?",
                (json.dumps(proposal), chat_id, user_id, message_id),
            )
            if result["status"] == "cancelled":
                db.execute(
                    "UPDATE messages SET state='cancelled' WHERE chat_id=? AND user_id=? "
                    "AND state IN ('pending','parsed')",
                    (chat_id, user_id),
                )
            return result

    def set_preview(self, chat_id, user_id, preview, *, version, generation):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_no_active_messages(db, chat_id, user_id)
            current = self._load(
                db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                ).fetchone()
            )
            if current is None or (current["version"], current["generation"]) != (
                version,
                generation,
            ):
                raise RevisionConflict("Получено новое сообщение; сверка устарела.")
            if current["status"] not in ("draft", "preview"):
                raise RevisionConflict("Запись уже начата или черновик завершён.")
            current.update(preview=preview, status="preview", generation=current["generation"] + 1)
            db.execute(
                "UPDATE drafts SET generation=?,payload=? WHERE chat_id=? AND user_id=?",
                (current["generation"], json.dumps(current), chat_id, user_id),
            )
            return current

    def begin_operation(self, chat_id, user_id, *, version, generation):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_no_active_messages(db, chat_id, user_id)
            current = self._load(
                db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                ).fetchone()
            )
            if current is None or (current["version"], current["generation"]) != (
                version,
                generation,
            ):
                raise RevisionConflict("Подтверждение устарело: перечитайте новый итог.")
            if current["status"] != "preview" or not current.get("preview"):
                raise RevisionConflict("Нужна актуальная сверка перед записью.")
            operation_id = f"{current['id']}:{version}"
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                (
                    operation_id,
                    chat_id,
                    user_id,
                    version,
                    "saving",
                    json.dumps(current["preview"]),
                    time.time(),
                ),
            )
            current.update(status="saving", operation_id=operation_id)
            db.execute(
                "UPDATE drafts SET payload=? WHERE chat_id=? AND user_id=?",
                (json.dumps(current), chat_id, user_id),
            )
            return operation_id

    def operation(self, operation_id, chat_id, user_id):
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE id=? AND chat_id=? AND user_id=?",
                (operation_id, chat_id, user_id),
            ).fetchone()
            return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def checkpoint(self, operation_id, chat_id, user_id, payload, state="saving"):
        if state not in ("saving", "uncertain", "saved"):
            raise ValueError("Invalid operation state")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE operations SET payload=?,state=?,updated_at=? WHERE id=? AND chat_id=? "
                "AND user_id=?",
                (json.dumps(payload), state, time.time(), operation_id, chat_id, user_id),
            ).rowcount
            if changed != 1:
                raise RevisionConflict("Операция не найдена.")
            current = self._load(
                db.execute(
                    "SELECT * FROM drafts WHERE chat_id=? AND user_id=?",
                    (chat_id, user_id),
                ).fetchone()
            )
            if current is None or current.get("operation_id") != operation_id:
                raise RevisionConflict("Операция не относится к текущему черновику.")
            current["status"] = state
            db.execute(
                "UPDATE drafts SET payload=? WHERE chat_id=? AND user_id=?",
                (json.dumps(current), chat_id, user_id),
            )
