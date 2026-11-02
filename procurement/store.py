"""Separate operational settings and order intentions, never accounting writes."""
import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from uuid import UUID
from .engine import ITEMS
from .engine import POINTS


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS policies(item TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,item TEXT NOT NULL,quantity REAL NOT NULL,
            expected_on TEXT NOT NULL,status TEXT NOT NULL,supplier TEXT NOT NULL,actor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS alert_log(event TEXT PRIMARY KEY,status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stock_counts(id TEXT PRIMARY KEY,item TEXT NOT NULL,location TEXT NOT NULL,
            quantity REAL NOT NULL,counted_on TEXT NOT NULL,note TEXT NOT NULL,actor INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,value TEXT NOT NULL);''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(orders)')}
            for column in ('received_on', 'location'):
                if column not in columns:
                    db.execute(f'ALTER TABLE orders ADD COLUMN {column} TEXT')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def policies(self):
        with self.connect() as db:
            return {r['item']: json.loads(r['payload']) for r in db.execute('SELECT * FROM policies')}

    def orders(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,item,quantity,expected_on,status,supplier,received_on,location FROM orders ORDER BY expected_on,id')]

    def counts(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,item,location,quantity,counted_on,note FROM stock_counts ORDER BY counted_on,rowid')]

    def count(self, payload, actor):
        if set(payload) != {'id', 'item', 'location', 'quantity', 'counted_on', 'note'}:
            raise ValueError('Некорректный подсчёт')
        UUID(payload['id'])
        if payload['item'] not in ITEMS or payload['location'] not in (*POINTS, 'Дома', 'Гараж'):
            raise ValueError('Неизвестный товар или место')
        if isinstance(payload['quantity'], bool):
            raise ValueError('Укажите количество')
        q = float(payload['quantity'])
        if not math.isfinite(q) or not 0 <= q <= 1000000:
            raise ValueError('Количество вне допустимого диапазона')
        counted = date.fromisoformat(payload['counted_on'])
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if counted > datetime.now(ZoneInfo('Europe/Moscow')).date():
            raise ValueError('Подсчёт не может быть в будущем')
        note = payload['note']
        if not isinstance(note, str) or len(note) > 240:
            raise ValueError('Некорректное основание подсчёта')
        with self.connect() as db:
            existing = db.execute('SELECT * FROM stock_counts WHERE id=?', (payload['id'],)).fetchone()
            if existing:
                if (existing['item'], existing['location'], existing['quantity'], existing['counted_on'], existing['note']) != (payload['item'],payload['location'],q,payload['counted_on'],note):
                    raise ValueError('Подсчёт уже изменился')
                return
            db.execute('INSERT INTO stock_counts VALUES (?,?,?,?,?,?,?)',
                       (payload['id'],payload['item'],payload['location'],q,payload['counted_on'],note,actor))

    def policy(self, payload):
        if set(payload) != {'item', 'lead_days', 'safety_days', 'pack_size', 'monthly_usage'} or payload['item'] not in ITEMS:
            raise ValueError('Некорректные настройки товара')
        clean = {}
        for key in ('lead_days', 'safety_days', 'pack_size', 'monthly_usage'):
            value = payload[key]
            if value is None or value == '':
                clean[key] = None
                continue
            if isinstance(value, bool):
                raise ValueError('Укажите число')
            value = float(value)
            limit = 365 if key.endswith('_days') else 1000000
            if not math.isfinite(value) or not 0 <= value <= limit or (key == 'pack_size' and value == 0):
                raise ValueError('Число вне допустимого диапазона')
            clean[key] = value
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO policies VALUES (?,?)', (payload['item'], json.dumps(clean)))

    def order(self, payload, actor):
        if set(payload) != {'id', 'item', 'quantity', 'expected_on', 'supplier'} or payload['item'] not in ITEMS:
            raise ValueError('Некорректный заказ')
        UUID(payload['id'])
        q = float(payload['quantity'])
        if isinstance(payload['quantity'], bool) or not math.isfinite(q) or not 0 < q <= 1000000:
            raise ValueError('Укажите положительное количество')
        date.fromisoformat(payload['expected_on'])
        supplier = payload['supplier']
        if not isinstance(supplier, str) or len(supplier) > 120:
            raise ValueError('Некорректный поставщик')
        with self.connect() as db:
            existing = db.execute('SELECT * FROM orders WHERE id=?', (payload['id'],)).fetchone()
            if existing:
                if (existing['item'], existing['quantity'], existing['expected_on'], existing['supplier']) != (payload['item'], q, payload['expected_on'], supplier):
                    raise ValueError('Заказ уже изменился')
                return
            db.execute('INSERT INTO orders(id,item,quantity,expected_on,status,supplier,actor) VALUES (?,?,?,?,?,?,?)', (payload['id'], payload['item'], q, payload['expected_on'], 'ordered', supplier, actor))

    def receive(self, payload):
        if set(payload) != {'id', 'received_on', 'location'} or payload['location'] not in (*POINTS, 'Дома', 'Гараж'):
            raise ValueError('Укажите место получения')
        from datetime import datetime
        from zoneinfo import ZoneInfo
        received = date.fromisoformat(payload['received_on'])
        if received > datetime.now(ZoneInfo('Europe/Moscow')).date():
            raise ValueError('Получение не может быть в будущем')
        with self.connect() as db:
            row = db.execute('SELECT * FROM orders WHERE id=?', (payload['id'],)).fetchone()
            if row and row['status'] == 'received' and (row['received_on'],row['location']) == (payload['received_on'],payload['location']):
                return
            if not row or row['status'] != 'ordered':
                raise ValueError('Заказ уже изменился')
            db.execute("UPDATE orders SET status='received',received_on=?,location=? WHERE id=?", (payload['received_on'], payload['location'], payload['id']))

    def cancel(self, order_id):
        with self.connect() as db:
            if not db.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='ordered'", (order_id,)).rowcount:
                raise ValueError('Заказ уже изменился')

    def notifications(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM preferences WHERE key='notifications'").fetchone()
        return json.loads(row[0]) if row else {'enabled': False}

    def set_notifications(self, actor, enabled):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?)', ('notifications', json.dumps({'enabled': enabled, 'recipient': actor})))

    def reserve_alert(self, event):
        with self.connect() as db:
            return db.execute("INSERT OR IGNORE INTO alert_log VALUES (?,'attempting')", (event,)).rowcount == 1

    def alert_result(self, event, status):
        with self.connect() as db:
            db.execute('UPDATE alert_log SET status=? WHERE event=?', (status, event))
