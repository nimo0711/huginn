"""HTTP trust and malformed-input boundaries, using only an isolated database."""
from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server


class ServerGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = patch.object(server, 'DB_PATH', self.root / 'board.db')
        self.db.start()
        self.addCleanup(self.db.stop)
        # sqlite's context manager commits but does not close the connection.
        # Keep real connections alive until test cleanup so Python 3.14 does not
        # report the pre-existing production lifetime issue as fixture leakage.
        real_connect = server.connect
        def fixture_connect():
            connection = real_connect()
            self.addCleanup(connection.close)
            return connection
        self.connections = patch.object(server, 'connect', side_effect=fixture_connect)
        self.connections.start()
        self.addCleanup(self.connections.stop)
        server.init_db()

    def handler(self, path, body=None, host='127.0.0.1:8790', origin=None):
        h = object.__new__(server.Handler)
        h.path = path
        h.headers = Message()
        if host is not None:
            h.headers['Host'] = host
        if origin is not None:
            h.headers['Origin'] = origin
        raw = json.dumps(body).encode()
        h.headers['Content-Type'] = 'application/json'
        h.headers['Content-Length'] = str(len(raw))
        h.rfile = BytesIO(raw)
        h.result = None
        h._json = lambda code, value: setattr(h, 'result', (code, value))
        h._send = lambda code, value, ctype: setattr(h, 'result', (code, value))
        return h

    def dispatch(self, h, method):
        try:
            getattr(h, 'do_' + method)()
        except Exception as exc:
            self.fail(f'{h.path[:100]} escaped with {type(exc).__name__}: {exc}')
        self.assertIsNotNone(h.result)
        return h.result

    def event_count(self):
        with server.connect() as db:
            return db.execute('SELECT count(*) FROM events').fetchone()[0]

    def test_foreign_host_cannot_read_any_board_route(self):
        paths = ['/', '/stories', '/assets/brand.svg', '/api/state', '/api/sessions', '/api/actions', '/doc/example.html', '/api/health']
        for path in paths:
            with self.subTest(path=path):
                h = self.handler(path, host='attacker.example:8790')
                h._doc = lambda rel: h._json(200, {'private_document': True})
                # Session scanning reads real user files and may start external queries.
                with patch.object(server.scan, 'all_sessions', return_value=[]), \
                     patch.object(server.local_runtime, 'attach'), \
                     patch.object(server.sprint, 'summary', return_value={}), \
                     patch.object(server.usage, 'summary', return_value={}):
                    self.assertEqual(self.dispatch(h, 'GET')[0], 403)

    def test_missing_or_duplicate_host_is_rejected(self):
        for duplicate in (False, True):
            h = self.handler('/api/state', host='127.0.0.1:8790' if duplicate else None)
            if duplicate:
                h.headers['Host'] = 'attacker.example:8790'
            self.assertEqual(self.dispatch(h, 'GET')[0], 403)

    def test_foreign_origin_cannot_read_even_with_local_host(self):
        for origin in ('http://attacker.example:8790', 'null'):
            h = self.handler('/api/state', origin=origin)
            self.assertEqual(self.dispatch(h, 'GET')[0], 403)

    def test_both_local_hosts_can_read_without_origin(self):
        for host in ('127.0.0.1:8790', 'localhost:8790'):
            self.assertEqual(self.dispatch(self.handler('/api/state', host=host), 'GET')[0], 200)

    def test_limit_rejects_nonpositive_malformed_empty_or_repeated_values(self):
        for query in ('abc', '-1', '0', '1.5', '', '1&limit=2', '9' * 5000):
            with self.subTest(query=query[:30]):
                h = self.handler('/api/state?limit=' + query)
                self.assertEqual(self.dispatch(h, 'GET')[0], 400)

    def test_limit_bounds_real_timeline_and_caps_large_positive_values(self):
        for n in range(4):
            server.add_event({'kind': 'log', 'story': 'QA-1', 'message': str(n), 'ts': n + 1})
        h = self.handler('/api/state?story=QA-1&limit=2')
        code, value = self.dispatch(h, 'GET')
        self.assertEqual(code, 200)
        self.assertEqual([x['message'] for x in value['timeline']], ['3', '2'])
        with patch.object(server, 'state', side_effect=lambda story, limit: {'bounded_limit': limit}):
            code, value = self.dispatch(self.handler('/api/state?limit=999999'), 'GET')
        self.assertEqual((code, value['bounded_limit']), (200, 2000))

    def test_nul_document_path_is_a_controlled_client_error(self):
        h = self.handler('/doc/invalid%00.html')
        with patch.object(server.os.path, 'expanduser', return_value=str(self.root)):
            self.assertEqual(self.dispatch(h, 'GET')[0], 400)

    def test_foreign_host_cannot_write_events_or_aliases(self):
        for path, body in [('/api/events', {'kind': 'log'}), ('/api/alias', {'src': 's', 'dst': 'changed'})]:
            h = self.handler(path, body, host='attacker.example:8790')
            self.assertEqual(self.dispatch(h, 'POST')[0], 403)
        self.assertEqual(self.event_count(), 0)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM aliases').fetchone()[0], 0)

    def test_originless_wb_event_batch_still_persists(self):
        h = self.handler('/api/events', [{'kind': 'log', 'story': 'QA-2', 'message': '한글 CLI 기록', 'ts': 123}])
        self.assertEqual(self.dispatch(h, 'POST')[0], 200)
        with server.connect() as db:
            row = db.execute('SELECT story, message, ts FROM events').fetchone()
        self.assertEqual(tuple(row), ('QA-2', '한글 CLI 기록', 123))

    def test_browser_origin_cannot_write_events(self):
        h = self.handler('/api/events', {'kind': 'log'}, origin='http://127.0.0.1:8790')
        self.assertEqual(self.dispatch(h, 'POST')[0], 403)
        self.assertEqual(self.event_count(), 0)

    def test_invalid_event_shape_or_timestamp_does_not_partially_save_batch(self):
        invalid = [None, True, 5, 'text', [], {'ts': []}, {'ts': True}, {'ts': 'nan'}, {'ts': 'Infinity'}, {'ts': -1}, {'kind': 'unknown'}]
        for event in invalid:
            with self.subTest(event=event):
                h = self.handler('/api/events', [{'kind': 'log', 'message': 'must not persist'}, event])
                self.assertEqual(self.dispatch(h, 'POST')[0], 400)
                self.assertEqual(self.event_count(), 0)

    def test_event_scalar_body_is_rejected(self):
        for body in (None, True, 'text', 42):
            self.assertEqual(self.dispatch(self.handler('/api/events', body), 'POST')[0], 400)

    def test_invalid_alias_input_is_rejected_without_recording(self):
        for body in (None, [], {'src': ['s']}, {'src': 's', 'dst': ['d']}, {'src': 's\0'}):
            with self.subTest(body=body):
                self.assertEqual(self.dispatch(self.handler('/api/alias', body), 'POST')[0], 400)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM aliases').fetchone()[0], 0)

    def test_local_alias_write_and_remove_remain_available(self):
        for dst in ('new name', ''):
            h = self.handler('/api/alias', {'src': 'session-1', 'dst': dst}, origin='http://127.0.0.1:8790')
            self.assertEqual(self.dispatch(h, 'POST')[0], 200)
            with server.connect() as db:
                row = db.execute('SELECT dst FROM aliases WHERE src = ?', ('session-1',)).fetchone()
            self.assertEqual(row[0] if row else '', dst)

    def test_invalid_body_framing_is_rejected_before_any_write(self):
        for path, body in [('/api/events', {'kind': 'log'}), ('/api/alias', {'src': 's', 'dst': 'd'})]:
            for length in ('-1', 'invalid', '0', str(2 * 1024 * 1024), None, 'duplicate'):
                with self.subTest(path=path, length=length):
                    h = self.handler(path, body)
                    if length == 'duplicate':
                        h.headers['Content-Length'] = h.headers['Content-Length']
                    else:
                        del h.headers['Content-Length']
                        if length is not None:
                            h.headers['Content-Length'] = length
                    self.assertEqual(self.dispatch(h, 'POST')[0], 400)
        self.assertEqual(self.event_count(), 0)

    def test_chunked_or_truncated_body_is_rejected(self):
        for framing in ('chunked', 'truncated'):
            h = self.handler('/api/events', {'kind': 'log'})
            if framing == 'chunked':
                h.headers['Transfer-Encoding'] = 'chunked'
            else:
                h.headers.replace_header('Content-Length', str(int(h.headers['Content-Length']) + 1))
            self.assertEqual(self.dispatch(h, 'POST')[0], 400)
        self.assertEqual(self.event_count(), 0)

    def test_invalid_request_target_is_a_controlled_client_error(self):
        for method in ('GET', 'POST'):
            h = self.handler('http://[invalid-host/api/state')
            self.assertEqual(self.dispatch(h, method)[0], 400)

    def test_excessively_nested_json_is_a_controlled_client_error(self):
        h = self.handler('/api/events')
        raw = b'[' * 1500 + b'0' + b']' * 1500
        h.headers.replace_header('Content-Length', str(len(raw)))
        h.rfile = BytesIO(raw)
        self.assertEqual(self.dispatch(h, 'POST')[0], 400)
        self.assertEqual(self.event_count(), 0)


if __name__ == '__main__':
    unittest.main()
