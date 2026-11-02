import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from app import Handler
from bot import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict(os.environ, {'MODE': 'paper', 'BOT_PASSWORD': 'test-fixture-only', 'REQUIRE_PERSISTENT_DATA': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.store = Store(self.folder.name)
        self.server.csrf = 'test-form-token'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, data=None, authenticated=True, origin=None):
        headers = {}
        if authenticated:
            headers['Authorization'] = 'Basic ' + base64.b64encode(b'misho:test-fixture-only').decode()
        if origin:
            headers['Origin'] = origin
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, data, headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        connection.close()
        return result

    def test_account_status_and_export_require_password(self):
        for path in ['/', '/api/status', '/export']:
            self.assertEqual(self.request('GET', path, authenticated=False)[0], 401)

    def test_export_contains_ledger_and_never_credentials(self):
        code, headers, body = self.request('GET', '/export')
        self.assertEqual(code, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        self.assertIn('ledger', json.loads(body))
        self.assertNotIn('test-fixture-only', body)
        self.assertNotIn('test-form-token', body)

    def test_pause_survives_restart_and_resume_responds_immediately(self):
        self.assertEqual(self.request('POST', '/toggle', 'csrf=test-form-token')[0], 303)
        self.assertTrue(Store(self.folder.name).state['paused'])
        self.assertIn('На пауза', self.request('GET', '/')[2])
        self.assertEqual(self.request('POST', '/toggle', 'csrf=test-form-token')[0], 303)
        status = json.loads(self.request('GET', '/api/status')[2])
        self.assertFalse(status['paused'])
        self.assertIn('Продължава', status['status'])

    def test_invalid_form_and_foreign_origin_cannot_change_pause(self):
        self.assertEqual(self.request('POST', '/toggle', 'csrf=wrong')[0], 403)
        self.assertEqual(self.request('POST', '/toggle', 'csrf=test-form-token', origin='https://example.org')[0], 403)
        self.assertFalse(self.server.store.state['paused'])

    def test_dead_engine_fails_health_check(self):
        self.server.worker = threading.Thread(target=lambda: None)
        code, _, body = self.request('GET', '/health', authenticated=False)
        self.assertEqual(code, 503)
        self.assertFalse(json.loads(body)['engine_alive'])

    def test_temporary_storage_is_visible_in_dashboard(self):
        page = self.request('GET', '/')[2]
        self.assertIn('Временна тестова среда', page)
        self.assertIn('Извършени проверки', page)
        self.assertIn('Последни проверки и решения', page)


if __name__ == '__main__':
    unittest.main()
