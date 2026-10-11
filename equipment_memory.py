"""Small equipment cases on the bot's existing persistent volume; no photo storage."""
import json
import re
import sqlite3
import time
from pathlib import Path

MEMORY_LIMITS = {'location': 50, 'model': 60, 'component': 20, 'problem': 500,
                 'error_code': 24, 'checks': 800, 'started_at': 100, 'last_answer': 1600}
TTL = 7 * 86400
MAX_CASES = 512
MAX_REPLY_LINKS = 2048


def starts_new_case(question):
    return bool(re.search(r'(?:нов|друг)(?:ая|ую)\s+(?:ошибк|проблем)|(?:новый|другой)\s+случай|забудь\s+предыдущ', question, re.I))


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
    """Call only after chat/staff authorization. Group replies share one incident.

    Negative Telegram chat IDs use a shared scope; private chats retain the
    employee key. Reply links resolve the latest card, including older replies.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS equipment_incidents '
                       '(user_id INTEGER, chat_id INTEGER, root_message_id INTEGER, updated REAL, '
                       'body TEXT, PRIMARY KEY(user_id, chat_id, root_message_id))')
            db.execute('CREATE TABLE IF NOT EXISTS equipment_reply_links '
                       '(user_id INTEGER, chat_id INTEGER, message_id INTEGER, root_message_id INTEGER, '
                       'PRIMARY KEY(user_id, chat_id, message_id), '
                       'FOREIGN KEY(user_id, chat_id, root_message_id) REFERENCES equipment_incidents '
                       '(user_id, chat_id, root_message_id) ON DELETE CASCADE)')
            # Previous versions stored per-employee snapshots. Import them once,
            # sharing only group cards and keeping the newest duplicate snapshot.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='equipment_cases'").fetchone():
                for user, chat, message, updated, body in db.execute(
                        'SELECT user_id, chat_id, message_id, updated, body FROM equipment_cases ORDER BY updated'):
                    user = self._scope(user, chat)
                    db.execute('INSERT INTO equipment_incidents VALUES (?,?,?,?,?) '
                               'ON CONFLICT(user_id,chat_id,root_message_id) DO UPDATE SET '
                               'updated=excluded.updated, body=excluded.body '
                               'WHERE excluded.updated > equipment_incidents.updated',
                               (user, chat, message, updated, body))
                    db.execute('INSERT OR REPLACE INTO equipment_reply_links VALUES (?,?,?,?)',
                               (user, chat, message, message))
                db.execute('DROP TABLE equipment_cases')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def _scope(user_id, chat_id):
        return 0 if chat_id < 0 else user_id

    @staticmethod
    def _prune(db, at):
        db.execute('DELETE FROM equipment_incidents WHERE updated < ?', (at - TTL,))
        db.execute('DELETE FROM equipment_incidents WHERE rowid NOT IN '
                   '(SELECT rowid FROM equipment_incidents ORDER BY updated DESC, rowid DESC LIMIT ?)',
                   (MAX_CASES,))
        db.execute('DELETE FROM equipment_reply_links WHERE rowid NOT IN '
                   '(SELECT rowid FROM equipment_reply_links ORDER BY rowid DESC LIMIT ?)',
                   (MAX_REPLY_LINKS,))

    @staticmethod
    def _find(db, user, chat, message):
        return db.execute('SELECT c.root_message_id,c.body FROM equipment_reply_links r '
                          'JOIN equipment_incidents c USING(user_id,chat_id,root_message_id) '
                          'WHERE r.user_id=? AND r.chat_id=? AND r.message_id=?',
                          (user, chat, message)).fetchone()

    def load(self, user_id, chat_id, message_id, *, now=None):
        at = time.time() if now is None else now
        with self._db() as db:
            self._prune(db, at)
            row = self._find(db, self._scope(user_id, chat_id), chat_id, message_id)
        return normalize_memory(json.loads(row[1])) if row else None

    def save(self, user_id, chat_id, message_id, memory, *, reply_message_id=None, start_new=False, now=None):
        memory = normalize_memory(memory)
        body = json.dumps(memory, ensure_ascii=False)
        at = time.time() if now is None else now
        user = self._scope(user_id, chat_id)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db, at)
            root = message_id
            if chat_id < 0 and reply_message_id and not start_new:
                parent = self._find(db, user, chat_id, reply_message_id)
                if parent:
                    previous = normalize_memory(json.loads(parent[1]))
                    if not previous.get('location') or previous['location'] == memory.get('location'):
                        root = parent[0]
            db.execute('INSERT INTO equipment_incidents VALUES (?,?,?,?,?) '
                       'ON CONFLICT(user_id,chat_id,root_message_id) DO UPDATE SET '
                       'updated=excluded.updated, body=excluded.body',
                       (user, chat_id, root, at, body))
            db.execute('INSERT OR REPLACE INTO equipment_reply_links VALUES (?,?,?,?)',
                       (user, chat_id, message_id, root))
            self._prune(db, at)
