"""Telegram-authenticated purchasing HTTP routes and local read-only preview."""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import ssl
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo
from .engine import build_dashboard
from .sources import SheetsSources
from .store import Store
from .purchasing import purchase_history
import certifi

WEB = Path(__file__).with_name('web')
logger = logging.getLogger(__name__)


def validate_init_data(raw, bot_token, owners, now=None):
    if not raw or len(raw) > 8192:
        raise PermissionError('Откройте закупщик из личного чата с ботом')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(data) != len(pairs):
        raise PermissionError('Повторные поля входа')
    supplied = data.pop('hash', '')
    check = '\n'.join(f'{k}={v}' for k, v in sorted(data.items()))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise PermissionError('Недействительный Telegram-вход')
    age = (now if now is not None else time.time()) - int(data.get('auth_date', 0))
    if not 0 <= age <= 3600:
        raise PermissionError('Вход устарел. Закройте и снова откройте закупщик')
    user = json.loads(data.get('user', '{}'))
    uid = user.get('id')
    if type(uid) is not int or uid not in owners:
        raise PermissionError('Закупщик доступен владельцу')
    return uid


class Workspace:
    def __init__(self, sources, store, bot_token, owners, preview=False):
        self.sources, self.store, self.bot_token, self.owners = sources, store, bot_token, owners
        self.preview = preview
        self.raw = None
        self.loading = False
        self.lock = threading.Lock()
        self.last_error = None
        self.sources.on_progress = self.progress

    def progress(self, raw):
        with self.lock:
            self.raw = raw

    def refresh(self):
        with self.lock:
            if self.loading:
                return
            self.loading = True
        def work():
            try:
                raw = self.sources.load()
                with self.lock:
                    self.raw, self.last_error = raw, None
            except Exception:
                logger.exception('Procurement source refresh failed')
                with self.lock:
                    self.last_error = 'Не удалось обновить источники. Сохранён предыдущий срез.'
            finally:
                with self.lock:
                    self.loading = False
        threading.Thread(target=work, daemon=True, name='procurement-refresh').start()

    def snapshot(self, month=None):
        with self.lock:
            raw, loading, error = self.raw, self.loading, self.last_error
        if raw is None:
            self.refresh()
            return {'loading': True, 'error': error, 'preview': self.preview}
        if time.monotonic() - self.sources.cached_at >= 900 and not loading:
            self.refresh()
        today = datetime.now(ZoneInfo('Europe/Moscow')).date()
        if month is not None:
            chosen = datetime.strptime(month, '%Y-%m').date()
            if chosen.replace(day=1) < today.replace(day=1) or not today.year <= chosen.year <= today.year + 1:
                raise ValueError('Выберите ближайший месяц')
        calculation_input = {**raw, 'manual_counts': self.store.counts()}
        result = build_dashboard(calculation_input, self.store.policies(), self.store.orders(), today=today, month=month)
        history, deliveries = purchase_history(raw.get('purchases', []), today.year)
        result.update({'loading': loading, 'error': error, 'preview': self.preview,
                       'purchase_history': history, 'delivery_history': deliveries,
                       'purchases': raw.get('purchases', []), 'purchases_tab': raw.get('purchases_tab'),
                       'notifications': self.store.notifications()})
        return result

    def service_photo(self, row):
        if type(row) is not int:
            raise ValueError('Некорректная фотография')
        with self.lock:
            raw = self.raw
        photo = next((p for p in (raw or {}).get('photos', []) if p['row'] == row), None)
        if not photo or not self.bot_token:
            raise ValueError('Фотография недоступна')
        context = ssl.create_default_context(cafile=certifi.where())
        try:
            url = 'https://api.telegram.org/bot' + self.bot_token + '/getFile?file_id=' + photo['file_id']
            with urllib.request.urlopen(url, timeout=15, context=context) as response:
                metadata = json.load(response)
            path = metadata['result']['file_path']
            if not path.startswith('photos/') or not path.lower().endswith(('.jpg', '.jpeg', '.png')):
                raise ValueError('Некорректный путь фотографии')
            url = 'https://api.telegram.org/file/bot' + self.bot_token + '/' + path
            with urllib.request.urlopen(url, timeout=20, context=context) as response:
                content = response.read(5_000_001)
            if len(content) > 5_000_000:
                raise ValueError('Фотография слишком велика')
            mime = 'image/png' if path.lower().endswith('.png') else 'image/jpeg'
            return {'image': 'data:' + mime + ';base64,' + base64.b64encode(content).decode()}
        except Exception:
            raise ValueError('Не удалось получить фотографию из Telegram') from None

    def handle(self, handler):
        path = urlsplit(handler.path).path
        if path not in ('/procurement', '/procurement/', '/procurement/app.js', '/procurement/style.css', '/api/procurement'):
            return False
        if path != '/api/procurement':
            if handler.command != 'GET':
                self.respond(handler, 405, {'error': 'Метод не поддерживается'})
                return True
            name = {'/procurement/app.js': 'app.js', '/procurement/style.css': 'style.css'}.get(path, 'index.html')
            mime = {'app.js': 'application/javascript', 'style.css': 'text/css', 'index.html': 'text/html'}[name]
            body = (WEB / name).read_bytes()
            handler.send_response(200)
            handler.send_header('Content-Type', mime + '; charset=utf-8')
            handler.send_header('Content-Length', str(len(body)))
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('Referrer-Policy', 'no-referrer')
            handler.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors https://web.telegram.org https://*.telegram.org")
            handler.end_headers()
            handler.wfile.write(body)
            return True
        if handler.command != 'POST':
            self.respond(handler, 405, {'error': 'Метод не поддерживается'})
            return True
        try:
            uid = 0 if self.preview else validate_init_data(handler.headers.get('X-Telegram-Init-Data', ''), self.bot_token, self.owners())
            size = int(handler.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192:
                raise ValueError('Некорректный размер запроса')
            data = json.loads(handler.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Некорректный запрос')
            action = data.pop('action', 'snapshot')
            if self.preview and action not in ('snapshot', 'refresh'):
                raise PermissionError('Локальный просмотр доступен только для чтения')
            if action == 'snapshot':
                if set(data) - {'month'}:
                    raise ValueError('Неизвестные поля')
                response = self.snapshot(data.get('month'))
            elif action == 'refresh':
                if not self.loading:
                    self.sources.cached_at = 0
                self.refresh()
                response = {'ok': True}
            elif action == 'photo' and set(data) == {'row'}:
                response = self.service_photo(data['row'])
            elif action == 'policy':
                self.store.policy(data)
                response = {'ok': True}
            elif action == 'count':
                self.store.count(data, uid)
                response = {'ok': True}
            elif action == 'order':
                self.store.order(data, uid)
                response = {'ok': True}
            elif action == 'receive_order':
                self.store.receive(data)
                response = {'ok': True}
            elif action == 'cancel_order' and set(data) == {'id'}:
                self.store.cancel(data['id'])
                response = {'ok': True}
            elif action == 'notifications' and set(data) == {'enabled'} and type(data['enabled']) is bool:
                self.store.set_notifications(uid, data['enabled'])
                response = {'ok': True}
            else:
                raise ValueError('Неизвестное действие')
            self.respond(handler, 200, response)
        except PermissionError as exc:
            self.respond(handler, 403, {'error': str(exc)})
        except (ValueError, KeyError, TypeError):
            self.respond(handler, 400, {'error': 'Проверьте поля запроса'})
        except Exception:
            logger.exception('Procurement request failed')
            self.respond(handler, 503, {'error': 'Временно недоступно. Попробуйте позже'})
        return True

    @staticmethod
    def respond(handler, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.end_headers()
        handler.wfile.write(body)


class ProcurementHttpServer:
    """Serve only the owner Mini App on its public-facing port."""

    def __init__(self, workspace, port, host='0.0.0.0'):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if not workspace.handle(self):
                    self.send_error(404)

            do_POST = do_GET

            def log_message(self, *_args):
                pass

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._server.daemon_threads = True
        self._thread = None

    @property
    def port(self):
        return self._server.server_port

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            daemon=True, name='procurement-http')
            self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def create_workspace(client_factory, primary_book, token, owners, path, preview=False):
    return Workspace(SheetsSources(client_factory, primary_book), Store(path), token, owners, preview)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true', help='Loopback-only read-only preview')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--snapshot', help='Recorded read-only Sheets snapshot for local preview')
    args = parser.parse_args()
    if args.snapshot and not args.preview:
        parser.error('--snapshot is available only with --preview')
    # Local env is never sent to the browser or printed.
    for envfile in (Path('.env'),):
        if envfile.exists():
            for line in envfile.read_text().splitlines():
                if '=' in line and not line.strip().startswith('#'):
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('\"\''))
    import gspread
    from google.oauth2.service_account import Credentials
    def client():
        scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
        info = os.getenv('CREDENTIALS_JSON')
        creds = Credentials.from_service_account_info(json.loads(info), scopes=scopes) if info else Credentials.from_service_account_file(os.getenv('CREDENTIALS_FILE', 'credentials.json'), scopes=scopes)
        result = gspread.authorize(creds)
        result.set_timeout((5, 20))
        return result
    owners = lambda: {int(x) for x in os.getenv('PROCUREMENT_OWNER_IDS', '').split(',') if x.strip()}
    workspace = create_workspace(client, os.getenv('SPREADSHEET_ID') or '1vz10vNFtOnJAD3J2eHCuxY1fF-8AsmmaXGQRymOsZFo', os.getenv('BOT_TOKEN', ''), owners, '.procurement/state.sqlite3', args.preview)
    workspace.sources.connected_snapshot = args.snapshot
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not workspace.handle(self):
                self.send_error(404)
        do_POST = do_GET
        def log_message(self, *args):
            pass
    workspace.refresh()
    print(f'Закупщик: http://127.0.0.1:{args.port}/procurement/', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
