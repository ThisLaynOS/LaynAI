"""Password-protected LAN control panel for the local OpenRC bot."""

import base64
import fcntl
import hmac
import ipaddress
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / 'data'
BACKUPS = ROOT / 'backups'
LOGS = Path('/var/log/laynai')
PAUSED = STATE / '.dashboard-paused'
LOCK = STATE / '.update.lock'
SERVICE = 'laynai'
PORT = 8787
CSRF = secrets.token_urlsafe(32)
PASSWORD_FILE = STATE / '.dashboard-password'
AUTH_PASSWORD = None
ALLOWED_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
}
LOG_FILES = {
    'bot': LOGS / 'bot.log',
    'errors': LOGS / 'bot.err',
    'updates': LOGS / 'update.log',
}


def ensure_password():
    """Create or reuse a private password stored outside Git."""
    STATE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STATE, 0o700)
    try:
        fd = os.open(PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w', encoding='ascii') as file:
            file.write(secrets.token_urlsafe(24) + '\n')
    os.chmod(PASSWORD_FILE, 0o600)
    password = PASSWORD_FILE.read_text(encoding='ascii').strip()
    if len(password) < 24:
        raise RuntimeError('Senha do painel inválida.')
    return password


def service_status():
    result = subprocess.run(('rc-service', SERVICE, 'status'), capture_output=True,
                            text=True, timeout=8, check=False)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def ready_status():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return {'ok': False, 'gateway': False, 'database': False}


def database_path():
    configured = os.environ.get('DATABASE_PATH')
    if not configured:
        for raw in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
            key, separator, value = raw.strip().partition('=')
            if separator and key.strip() == 'DATABASE_PATH':
                configured = value.strip().strip('"\'')
                break
    return (ROOT / (configured or './data/bot.sqlite')).resolve()


def usage_today():
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    path = database_path()
    if not path.is_file():
        return {'requests': 0, 'tokens': 0, 'calls': 0}
    with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True, timeout=2)) as db:
        requests = db.execute("SELECT count FROM usage WHERE day=? AND subject='*'", (day,)).fetchone()
        tokens = db.execute("SELECT total_tokens,calls FROM token_usage WHERE day=? AND subject='*'", (day,)).fetchone()
    return {'requests': requests[0] if requests else 0,
            'tokens': tokens[0] if tokens else 0,
            'calls': tokens[1] if tokens else 0}


def backups_list():
    if not BACKUPS.is_dir():
        return []
    items = []
    for path in BACKUPS.iterdir():
        if path.name == 'restorepoints' and path.is_dir():
            candidates = (p for p in path.iterdir() if p.is_dir() and not p.name.startswith('.'))
        elif path.is_file() and path.suffix == '.sqlite':
            candidates = (path,)
        else:
            continue
        for item in candidates:
            stat = item.stat()
            if item.is_dir():
                files = (p for p in item.iterdir() if p.is_file())
                size = sum(p.stat().st_size for p in files)
                revision = (item / 'revision').read_text().strip()[:12] if (item / 'revision').is_file() else '—'
                kind = 'Ponto completo'
            else:
                size = stat.st_size
                revision = '—'
                kind = 'Só banco'
            items.append({'name': item.name, 'kind': kind, 'revision': revision,
                          'size': size, 'created': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return sorted(items, key=lambda item: item['created'], reverse=True)[:40]


def tail_log(source):
    path = LOG_FILES[source]
    try:
        with path.open('rb') as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - 65536))
            data = file.read()
    except FileNotFoundError:
        return ''
    result = data.decode('utf-8', 'replace')
    if size > 65536:
        result = result.split('\n', 1)[-1]
    return '\n'.join(result.splitlines()[-180:])


def current_revision():
    result = subprocess.run(('git', 'rev-parse', '--short', 'HEAD'), cwd=ROOT,
                            capture_output=True, text=True, timeout=5, check=True)
    return result.stdout.strip()


def run_service(action):
    result = subprocess.run(('rc-service', SERVICE, action), cwd=ROOT, capture_output=True,
                            text=True, timeout=65, check=False, start_new_session=True)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip() or f'Falha em {action}.')


def boot_enabled():
    return (Path('/etc/runlevels/default') / SERVICE).is_symlink()


def set_boot_enabled(enabled):
    if boot_enabled() == enabled:
        return
    action = 'add' if enabled else 'del'
    result = subprocess.run(('rc-update', action, SERVICE, 'default'), cwd=ROOT,
                            capture_output=True, text=True, timeout=30, check=False,
                            start_new_session=True)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip() or 'Falha ao alterar início automático.')


def wait_ready(timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_status().get('ok') and service_status()[0]:
            return True
        time.sleep(1)
    return False


def perform_action(action):
    if action not in ('start', 'stop', 'restart'):
        raise ValueError('Ação inválida.')
    STATE.mkdir(mode=0o700, exist_ok=True)
    with LOCK.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Uma atualização está em andamento; tente novamente em instantes.') from error
        if action == 'stop':
            PAUSED.write_text('paused by dashboard\n')
            try:
                run_service('stop')
            except Exception:
                PAUSED.unlink(missing_ok=True)
                raise
            set_boot_enabled(False)
        else:
            run_service(action)
            if not wait_ready():
                raise RuntimeError('O serviço não ficou pronto; confira os logs.')
            set_boot_enabled(True)
            PAUSED.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print('%s %s' % (self.address_string(), format % args), flush=True)

    def _allowed_host(self):
        try:
            host = urllib.parse.urlsplit('//' + self.headers.get('Host', ''))
            if host.port != self.server.server_port:
                return False
            if host.hostname == 'localhost':
                return ipaddress.ip_address(self.client_address[0]).is_loopback
            return ipaddress.ip_address(host.hostname).version == 4
        except (ValueError, TypeError):
            return False

    def _allowed_client(self):
        address = ipaddress.ip_address(self.client_address[0])
        return any(address in network for network in ALLOWED_NETWORKS)

    def _authenticated(self):
        if self.path == '/healthz' and ipaddress.ip_address(self.client_address[0]).is_loopback:
            return True
        if not getattr(self.server, 'auth_required', True):
            return True
        header = self.headers.get('Authorization', '')
        if not header.startswith('Basic '):
            return False
        try:
            credentials = base64.b64decode(header[6:], validate=True).decode('utf-8')
        except (ValueError, UnicodeError):
            return False
        username, separator, password = credentials.partition(':')
        return separator == ':' and hmac.compare_digest(username, 'admin') and hmac.compare_digest(password, AUTH_PASSWORD)

    def _send(self, status, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if status == 401:
            self.send_header('WWW-Authenticate', 'Basic realm="LaynAI Dashboard", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._allowed_client():
            return self._send(403, {'error': 'Acesso permitido apenas na rede local.'})
        if not self._allowed_host():
            return self._send(403, {'error': 'Host não permitido.'})
        if not self._authenticated():
            return self._send(401, {'error': 'Autenticação necessária.'})
        path = urllib.parse.urlsplit(self.path)
        try:
            if path.path == '/healthz':
                return self._send(200, {'ok': True})
            if path.path in ASSETS:
                name, content_type = ASSETS[path.path]
                return self._send(200, (ROOT / 'dashboard' / name).read_bytes(), content_type)
            if path.path == '/api/status':
                running, detail = service_status()
                try:
                    usage = usage_today()
                except (OSError, sqlite3.Error, ValueError):
                    usage = {'requests': 0, 'tokens': 0, 'calls': 0}
                return self._send(200, {'running': running, 'service': detail,
                                        'ready': ready_status(), 'paused': PAUSED.exists(),
                                        'revision': current_revision(), 'usage': usage})
            if path.path == '/api/backups':
                return self._send(200, {'items': backups_list()})
            if path.path == '/api/logs':
                source = urllib.parse.parse_qs(path.query).get('source', ['bot'])[0]
                if source not in LOG_FILES:
                    return self._send(400, {'error': 'Log inválido.'})
                return self._send(200, {'source': source, 'text': tail_log(source)})
            if path.path == '/api/csrf':
                return self._send(200, {'token': CSRF})
            return self._send(404, {'error': 'Não encontrado.'})
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            print(f'Erro GET {path.path}: {error}', file=sys.stderr, flush=True)
            return self._send(500, {'error': 'Falha ao carregar dados.'})

    def do_POST(self):
        if not self._allowed_client():
            return self._send(403, {'error': 'Acesso permitido apenas na rede local.'})
        if not self._allowed_host():
            return self._send(403, {'error': 'Host não permitido.'})
        if not self._authenticated():
            return self._send(401, {'error': 'Autenticação necessária.'})
        origin = self.headers.get('Origin')
        scheme = 'https' if getattr(self.server, 'secure', True) else 'http'
        allowed_origin = f'{scheme}://{self.headers.get("Host")}'
        if origin != allowed_origin or self.headers.get('X-CSRF-Token') != CSRF:
            return self._send(403, {'error': 'Requisição não autorizada.'})
        if self.path != '/api/action' or self.headers.get('Content-Type') != 'application/json':
            return self._send(404, {'error': 'Não encontrado.'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                return self._send(400, {'error': 'Corpo inválido.'})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                return self._send(400, {'error': 'Ação inválida.'})
            action = data.get('action')
            perform_action(action)
            return self._send(200, {'ok': True, 'action': action})
        except (ValueError, TypeError, json.JSONDecodeError):
            return self._send(400, {'error': 'Ação inválida.'})
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            print(f'Erro POST: {error}', file=sys.stderr, flush=True)
            return self._send(500, {'error': str(error)[:250]})


def main():
    global AUTH_PASSWORD
    AUTH_PASSWORD = ensure_password()
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    server.auth_required = True
    server.secure = False
    print(f'Dashboard LaynAI HTTP em 0.0.0.0:{PORT} (somente rede local)', flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
