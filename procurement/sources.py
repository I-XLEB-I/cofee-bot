"""Read-only adapters for existing Sheets and official Telemetron API v2."""
from __future__ import annotations
import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .engine import POINTS, day, number
from .revisions import BDR_ID, merge_revisions, pending_reports
import certifi

COST_BOOK = '1FHHXLQ3MVMQMGuXFGbDHNEB5tNf3wQKiwz-X7OXumVM'
PURCHASE_BOOK = '1L-fRh7Ry2C3-xH0Bbkc-QxSLhuDzN-NA1suowCX2RPE'
PRIMARY_BOOK = '1vz10vNFtOnJAD3J2eHCuxY1fF-8AsmmaXGQRymOsZFo'


class TelemetronSales:
    def __init__(self):
        self.token = None
        self.expires = 0
        self.lock = threading.Lock()

    def request(self, path, payload=None, query=None):
        url = 'https://api.telemetron.net' + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        headers = {'Accept': 'application/json'}
        if self.token and path != '/auth/':
            headers['Authorization'] = 'Bearer ' + self.token
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=25, context=ssl.create_default_context(cafile=certifi.where())) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('Telemetron response too large')
        return json.loads(raw)

    def authenticate(self):
        if self.token and self.expires > time.monotonic():
            return
        fields = {key: os.getenv('TELEMETRON_' + key.upper(), '') for key in ('client_id', 'client_secret', 'username', 'password')}
        if not all(fields.values()):
            raise ValueError('Telemetron не подключён на сервере закупщика')
        data = self.request('/auth/', {**fields, 'grant_type': 'password', 'scope': 'teleport'})
        if not isinstance(data.get('access_token'), str) or not data['access_token']:
            raise ValueError('Telemetron authentication failed')
        self.token = data['access_token']
        self.expires = time.monotonic() + max(0, int(data.get('expires_in', 3600)) - 60)

    def points(self):
        with self.lock:
            self.authenticate()
            rows = self.request('/v2/vending_machines')
            if not isinstance(rows, list):
                raise ValueError('Invalid Telemetron point list')
            return rows

    def sales(self, point, since, until):
        totals = {}
        daily = {}
        daily_complete = True
        with self.lock:
            self.authenticate()
            cursor = since
            while cursor <= until:
                end = min(cursor + timedelta(days=30), until)
                for vend_type in ('paid', 'free', 'test'):
                    rows = self.request(f'/v2/vending_machines/{POINTS[point]}/stats/vends/products', query={
                        'since': cursor.isoformat() + 'T00:00:00+03:00',
                        'until': end.isoformat() + 'T23:59:59+03:00', 'type': vend_type})
                    if not isinstance(rows, list):
                        raise ValueError('Invalid Telemetron sales')
                    for row in rows:
                        name = str(row.get('name') or '')
                        count = number(row.get('number'))
                        if not name or count is None:
                            raise ValueError('Invalid Telemetron product row')
                        totals[name] = totals.get(name, 0) + count
                        sold_on = day(str(row.get('time', ''))[:10])
                        if sold_on and cursor <= sold_on <= end:
                            bucket = daily.setdefault(sold_on.isoformat(), {})
                            bucket[name] = bucket.get(name, 0) + count
                        else:
                            daily_complete = False
                cursor = end + timedelta(days=1)
        return {'point': point, 'since': since.isoformat(), 'until': until.isoformat(),
                'products': [{'name': k, 'sales_count': v} for k, v in totals.items()],
                'daily_products': daily, 'daily_complete': daily_complete}


class SheetsSources:
    def __init__(self, client_factory, primary_book=PRIMARY_BOOK, connected_snapshot=None):
        self.client_factory, self.primary_book = client_factory, primary_book
        self.sales_client = TelemetronSales()
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0
        self.connected_snapshot = connected_snapshot
        self.on_progress = None

    def publish(self, raw):
        if self.on_progress:
            self.on_progress(json.loads(json.dumps(raw)))

    def load(self):
        with self.lock:
            if self.cached is not None and time.monotonic() - self.cached_at < 900:
                return self.cached
            client = self.client_factory()
            today = datetime.now(ZoneInfo('Europe/Moscow')).date()
            warnings, sources = [], []
            raw = {'revisions': [], 'recipes': [], 'movements': [], 'sales': [], 'since_revision_sales': [],
                   'purchases': [], 'warnings': warnings, 'sources': sources, 'active_points': list(POINTS),
                   'movement_coverage_confirmed': False, 'fetched_at': datetime.now(ZoneInfo('Europe/Moscow')).isoformat()}
            def read(book_id, tab, range_name):
                try:
                    rows = client.open_by_key(book_id).worksheet(tab).get(range_name, value_render_option='UNFORMATTED_VALUE')
                    sources.append({'name': tab, 'url': f'https://docs.google.com/spreadsheets/d/{book_id}/edit', 'available': True})
                    return rows
                except Exception:
                    # Do not leak provider response/credentials into the owner UI.
                    warnings.append(f'Не удалось прочитать источник «{tab}»')
                    sources.append({'name': tab, 'url': f'https://docs.google.com/spreadsheets/d/{book_id}/edit', 'available': False})
                    return []
            rows = read(self.primary_book, 'Ревизия', 'A1:U3000')
            if rows:
                raw['revisions'] = [dict(zip(rows[0], r)) for r in rows[1:] if any(str(x).strip() for x in r)]
            primary_revisions = list(raw['revisions'])
            bdr = read(BDR_ID, 'Ревизия', 'A1:Z537')
            if bdr:
                raw['revisions'], reconciliation_issues = merge_revisions(primary_revisions, bdr)
                warnings.extend(reconciliation_issues)
                sources[-1]['name'] = 'БДР · Ревизия'
            group_journal = read(self.primary_book, 'Импорт группы', 'A1:Q2000')
            raw['pending_revisions'] = pending_reports(group_journal)
            service = read(self.primary_book, 'Обслуживание', 'A1:J1200')
            raw['service_reports'] = [dict(zip(service[0], row), _row=index)
                                      for index, row in enumerate(service[1:], start=2)
                                      if len(row) >= 3 and day(row[0]) and row[2] in POINTS] if service else []
            photos = read(self.primary_book, 'Фото', 'A1:D1200')
            raw['photos'] = [{'row': index, 'date': day(row[0]).isoformat(), 'point': row[1], 'file_id': row[3]}
                             for index, row in enumerate(photos[1:], start=2)
                             if len(row) >= 4 and day(row[0]) and row[1] in POINTS and isinstance(row[3], str)] if photos else []
            if service and group_journal:
                for report in raw['pending_revisions']:
                    linked = dict(zip(group_journal[0], group_journal[report['row'] - 1]))
                    try:
                        service_row = service[int(linked.get('Service_Row') or 0) - 1]
                        service_day = day(service_row[0]) if service_row else None
                        if len(service_row) < 3 or service_row[2] != report['point'] or not service_day or service_day.isoformat() != report['date']:
                            report['verification_issue'] = 'Ссылка журнала на обслуживание указывает другую точку или дату'
                    except (IndexError, TypeError, ValueError):
                        report['verification_issue'] = 'Связанный отчёт обслуживания не найден'
                    if report.get('verification_issue'):
                        warnings.append(f'Импорт группы, строка {report["row"]}: {report["verification_issue"]}')
            self.publish(raw)
            raw['costing'] = read(COST_BOOK, '2026_Себестоимость', 'A1:J100')
            raw['recipes'] = read(COST_BOOK, '2026_Рецептуры', 'A1:T200')
            moves = read(self.primary_book, 'Движения товаров', 'A1:K20000')
            for row in moves[1:]:
                if len(row) < 9 or row[4] != 'Учтено':
                    continue
                try:
                    items = json.loads(row[8])
                    if not isinstance(items, list):
                        raise ValueError()
                    for item in items:
                        raw['movements'].append({'date': row[0], 'point': row[1], 'item': item['item'],
                                                 'source': item.get('source'), 'quantity': item.get('balance_quantity')})
                except (ValueError, KeyError, TypeError):
                    warnings.append('Часть движений не удалось разобрать; остатки неполные')
            # Existing financial books remain read-only. Goods purchases are evidence, not automatically receipts.
            purchase_tab = f'{today.month - 1 if today.month > 1 else 12}.{str(today.year if today.month > 1 else today.year - 1)[2:]}'
            raw['purchases'] = read(PURCHASE_BOOK, purchase_tab, 'A1:I200')
            if self.connected_snapshot:
                recorded = json.loads(Path(self.connected_snapshot).read_text())
                for key, tab in (('costing', '2026_Себестоимость'), ('recipes', '2026_Рецептуры'), ('purchases', purchase_tab)):
                    if not raw[key] and recorded.get(key):
                        raw[key] = recorded[key]
                        snapshot_at = recorded.get(key + '_fetched_at', recorded['fetched_at'])
                        warnings.append(f'«{tab}»: сохранённый срез Google Drive от {snapshot_at}; автоматическое чтение недоступно')
                        for source in sources:
                            if source['name'] == tab:
                                source['available'] = True
                                source['snapshot_at'] = snapshot_at
            self.publish(raw)
            try:
                points = self.sales_client.points()
                # Provider state 3 denotes disabled, as used in the existing integration.
                enabled = {int(p['id']) for p in points if p.get('state') != 3 and p.get('id')}
                raw['active_points'] = [name for name, pid in POINTS.items() if pid in enabled]
                if not raw['active_points']:
                    raise ValueError('No configured active points')
            except Exception:
                warnings.append('Не удалось подтвердить действующие точки Telemetron; прогноз по сети ограничен')
            since, until = today - timedelta(days=28), today - timedelta(days=1)
            raw['sales_window'] = {'since': since.isoformat(), 'until': until.isoformat()}
            for point in raw['active_points']:
                try:
                    raw['sales'].append(self.sales_client.sales(point, since, until))
                    candidates = [r for r in primary_revisions if r.get('Локация') == point and day(r.get('Дата заполнения'))]
                    latest = max(candidates, key=lambda r: day(r['Дата заполнения'])) if candidates else None
                    counted = day(latest['Дата заполнения']) if latest else None
                    if counted and today - timedelta(days=93) <= counted < until:
                        raw['since_revision_sales'].append(self.sales_client.sales(point, counted + timedelta(days=1), until))
                except Exception:
                    warnings.append(f'Продажи {point}: источник недоступен; расход не заменён нулём')
                self.publish(raw)
            raw['purchases_tab'] = purchase_tab
            self.cached, self.cached_at = raw, time.monotonic()
            return raw
