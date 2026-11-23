import contextlib
import http.client
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import desktop_service
import failure_log
import server


class ServiceLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='huginn-log-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        streams = patch.multiple(sys, stdout=sys.stdout, stderr=sys.stderr)
        streams.start()
        self.addCleanup(streams.stop)

    def close_log(self):
        sys.stderr.close()

    def test_service_output_goes_to_a_log_in_the_data_folder(self):
        path = desktop_service.attach_service_log(self.root / '보드 자료')
        print('표준 출력 줄')
        print('오류 출력 줄', file=sys.stderr)
        self.close_log()
        text = path.read_text(encoding='utf-8')
        self.assertIn('서비스 시작 pid=', text)
        self.assertIn('표준 출력 줄', text)
        self.assertIn('오류 출력 줄', text)

    def test_a_full_log_moves_aside_before_the_service_writes_again(self):
        old = self.root / 'service.log'
        old.write_text('이전 기록\n' * 10, encoding='utf-8')
        path = desktop_service.attach_service_log(self.root, limit=20)
        self.close_log()
        self.assertEqual((self.root / 'service.log.1').read_text(encoding='utf-8'), '이전 기록\n' * 10)
        self.assertNotIn('이전 기록', path.read_text(encoding='utf-8'))

    def test_an_unhandled_request_error_is_logged_with_its_time_and_traceback(self):
        class Broken(server.Handler):
            def do_GET(self):
                raise RuntimeError('처리 중 실패')

        captured = io.StringIO()
        with server.LoopbackServer(('127.0.0.1', 0), Broken) as httpd:
            worker = threading.Thread(target=httpd.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.object(sys, 'stderr', captured):
                    with contextlib.closing(http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=2)) as conn:
                        conn.request('GET', '/api/health')
                        with self.assertRaises((http.client.RemoteDisconnected, ConnectionError)):
                            conn.getresponse()
            finally:
                httpd.shutdown()
                worker.join()
        text = captured.getvalue()
        self.assertRegex(text, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} 요청 처리 오류')
        self.assertIn('RuntimeError: 처리 중 실패', text)

    def test_a_hidden_failure_is_reported_once_with_its_place_and_cause(self):
        captured = io.StringIO()
        with patch.object(sys, 'stderr', captured), patch.object(failure_log, '_reported', set()):
            for _ in range(3):
                failure_log.report('Codex 작업 기록 읽기', OSError('디스크 오류'))
            failure_log.report('Codex 작업 기록 읽기', OSError('다른 오류'))
        lines = captured.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} Codex 작업 기록 읽기: OSError: 디스크 오류$')

    def test_a_dropped_spool_line_is_reported_instead_of_vanishing(self):
        spool = self.root / 'spool'
        spool.mkdir()
        (spool / '1.jsonl').write_text('{"kind": "story", "story": "T-1"}\n깨진 줄\n', encoding='utf-8')
        captured = io.StringIO()
        added = []

        def stop(_):
            raise SystemExit

        with patch.object(server, 'SPOOL', str(spool)), patch.object(server, 'add_event', added.append), \
                patch.object(server.time, 'sleep', stop), patch.object(sys, 'stderr', captured), \
                patch.object(failure_log, '_reported', set()):
            with self.assertRaises(SystemExit):
                server.drain_spool()
        self.assertEqual(added, [{'kind': 'story', 'story': 'T-1'}])
        self.assertIn('전송 대기 기록 1.jsonl의 줄을 버림: JSONDecodeError', captured.getvalue())


if __name__ == '__main__':
    unittest.main()
