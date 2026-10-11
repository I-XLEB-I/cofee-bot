"""Small equipment cases on the bot's existing persistent volume; no photo storage."""
import json
import sqlite3
import time
from pathlib import Path

MEMORY_LIMITS = {'location': 50, 'model': 60, 'component': 20, 'problem': 500,
                 'error_code': 24, 'checks': 800, 'started_at': 100, 'last_answer': 1600}
TTL = 7 * 86400


def normalize_memory(value):
    if not isinstance(value, dict) or set(value) - set(MEMORY_LIMITS) - {'urgent'}:
        raise ValueError('Invalid equipment memory')
    result = {}
    for key, item in value.items():
        if key == 'urgent':
            if type(item) is not bool:
                raise ValueError('Invalid equipment urgency')
        elif item is not None and (not isinstance(item, str) or len(item) > MEMORY_LIMITS[key]):
            raise ValueError('Equipment memory exceeds limits')
        result[key] = item
    return result


class EquipmentMemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS equipment_cases '
                       '(user_id INTEGER, chat_id INTEGER, message_id INTEGER, updated REAL, '
                       'body TEXT, PRIMARY KEY(user_id, chat_id, message_id))')

    def _db(self):
        return sqlite3.connect(self.path, timeout=3)

    def load(self, user_id, chat_id, message_id, *, now=None):
        at = time.time() if now is None else now
        with self._db() as db:
            db.execute('DELETE FROM equipment_cases WHERE updated < ?', (at - TTL,))
            row = db.execute('SELECT body FROM equipment_cases WHERE user_id=? AND chat_id=? AND message_id=?',
                             (user_id, chat_id, message_id)).fetchone()
        return normalize_memory(json.loads(row[0])) if row else None

    def save(self, user_id, chat_id, message_id, memory, *, now=None):
        body = json.dumps(normalize_memory(memory), ensure_ascii=False)
        at = time.time() if now is None else now
        with self._db() as db:
            db.execute('DELETE FROM equipment_cases WHERE updated < ?', (at - TTL,))
            db.execute('INSERT OR REPLACE INTO equipment_cases VALUES (?,?,?,?,?)',
                       (user_id, chat_id, message_id, at, body))
            db.execute('DELETE FROM equipment_cases WHERE rowid NOT IN '
                       '(SELECT rowid FROM equipment_cases ORDER BY updated DESC LIMIT 512)')
