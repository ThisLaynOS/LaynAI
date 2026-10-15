import base64
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard import server as dashboard


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
        self.server.secure = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers={
                'Host': f'127.0.0.1:{self.port}', **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_healthz_and_host_restriction(self):
        self.assertEqual(self.request('GET', '/healthz'), (200, {'ok': True}))
        status, _ = self.request('GET', '/healthz', headers={'Host': 'evil.example'})
        self.assertEqual(status, 403)

    def test_remote_access_requires_password_and_numeric_host(self):
        self.server.secure = True
        with patch.object(dashboard, 'AUTH_PASSWORD', 'test-password'):
            status, _ = self.request('GET', '/api/csrf')
            self.assertEqual(status, 401)
            wrong = base64.b64encode(b'admin:wrong').decode()
            status, _ = self.request('GET', '/api/csrf', headers={'Authorization': f'Basic {wrong}'})
            self.assertEqual(status, 401)
            correct = base64.b64encode(b'admin:test-password').decode()
            status, data = self.request('GET', '/api/csrf', headers={
                'Authorization': f'Basic {correct}', 'Host': f'192.168.1.42:{self.port}'})
            self.assertEqual(status, 200)
            self.assertIn('token', data)
            status, _ = self.request('GET', '/api/csrf', headers={
                'Authorization': f'Basic {correct}', 'Host': f'evil.example:{self.port}'})
            self.assertEqual(status, 403)

    def test_secure_post_requires_matching_https_origin(self):
        self.server.secure = True
        body = json.dumps({'action': 'restart'})
        authorization = 'Basic ' + base64.b64encode(b'admin:test-password').decode()
        headers = {'Authorization': authorization, 'Host': f'192.168.1.42:{self.port}',
                   'Content-Type': 'application/json', 'X-CSRF-Token': dashboard.CSRF}
        with patch.object(dashboard, 'AUTH_PASSWORD', 'test-password'), \
                patch.object(dashboard, 'perform_action') as action:
            status, _ = self.request('POST', '/api/action', body, {
                **headers, 'Origin': f'http://192.168.1.42:{self.port}'})
            self.assertEqual(status, 403)
            status, data = self.request('POST', '/api/action', body, {
                **headers, 'Origin': f'https://192.168.1.42:{self.port}'})
            self.assertEqual(status, 200)
            self.assertEqual(data['action'], 'restart')
        action.assert_called_once_with('restart')

    def test_security_files_are_private_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(dashboard, 'STATE', root), \
                    patch.object(dashboard, 'PASSWORD_FILE', root / '.dashboard-password'), \
                    patch.object(dashboard, 'CERT_FILE', root / 'dashboard.crt'), \
                    patch.object(dashboard, 'KEY_FILE', root / 'dashboard.key'):
                password = dashboard.ensure_security_files()
                self.assertEqual(password, dashboard.ensure_security_files())
                self.assertGreaterEqual(len(password), 24)
                self.assertTrue(dashboard.CERT_FILE.is_file())
                self.assertEqual(dashboard.KEY_FILE.stat().st_mode & 0o777, 0o600)
                self.assertEqual(dashboard.PASSWORD_FILE.stat().st_mode & 0o777, 0o600)

    def test_action_requires_local_origin_and_csrf(self):
        token = self.request('GET', '/api/csrf')[1]['token']
        body = json.dumps({'action': 'stop'})
        with patch.object(dashboard, 'perform_action') as action:
            status, _ = self.request('POST', '/api/action', body, {'Content-Type': 'application/json'})
            self.assertEqual(status, 403)
            status, _ = self.request('POST', '/api/action', body, {
                'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}',
                'X-CSRF-Token': 'wrong'})
            self.assertEqual(status, 403)
            status, data = self.request('POST', '/api/action', body, {
                'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.port}',
                'X-CSRF-Token': token})
            self.assertEqual(status, 200)
            self.assertEqual(data['action'], 'stop')
        action.assert_called_once_with('stop')

    def test_log_source_is_allowlisted(self):
        status, _ = self.request('GET', '/api/logs?source=../../etc/passwd')
        self.assertEqual(status, 400)

    def test_backups_list_does_not_expose_env_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            point = root / 'restorepoints' / '20260101T000000Z-aabbccdd'
            point.mkdir(parents=True)
            (point / 'revision').write_text('a' * 40)
            (point / '.env').write_text('SECRET=do-not-expose')
            (point / 'bot.sqlite').write_bytes(b'database')
            with patch.object(dashboard, 'BACKUPS', root):
                status, data = self.request('GET', '/api/backups')
            self.assertEqual(status, 200)
            self.assertEqual(data['items'][0]['kind'], 'Ponto completo')
            self.assertNotIn('do-not-expose', json.dumps(data))

    def test_stop_and_start_pause_updates_and_change_boot_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(dashboard, 'STATE', root), \
                    patch.object(dashboard, 'PAUSED', root / '.dashboard-paused'), \
                    patch.object(dashboard, 'LOCK', root / '.update.lock'), \
                    patch.object(dashboard, 'run_service') as service, \
                    patch.object(dashboard, 'wait_ready', return_value=True), \
                    patch.object(dashboard, 'set_boot_enabled') as boot:
                dashboard.perform_action('stop')
                self.assertTrue(dashboard.PAUSED.exists())
                service.assert_called_with('stop')
                boot.assert_called_with(False)
                dashboard.perform_action('start')
                self.assertFalse(dashboard.PAUSED.exists())
                service.assert_called_with('start')
                boot.assert_called_with(True)


if __name__ == '__main__':
    unittest.main()
