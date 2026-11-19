import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVICE = [sys.executable, str(ROOT / 'desktop_service.py')]


class ReleaseRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='huginn-service-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / '테스트 사용자'
        self.home.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('WORK_BOARD_', 'HUGINN_'))}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home))

    def service(self, *args):
        return subprocess.run([*SERVICE, *args], env=self.env, cwd=self.root,
                              capture_output=True, text=True, encoding='utf-8', timeout=15)

    def test_loopback_service_starts_without_reverse_dns(self):
        import server
        with patch('socket.getfqdn', side_effect=AssertionError('Local service must not wait for DNS')):
            with server.LoopbackServer(('127.0.0.1', 0), server.Handler) as httpd:
                self.assertEqual(httpd.server_name, '127.0.0.1')
                self.assertGreater(httpd.server_port, 0)

    def test_source_paths_and_explicit_overrides(self):
        self.env.update(HUGINN_DATA_DIR=str(self.root / '보드 자료'),
                        WORK_BOARD_DB=str(self.root / '다른 기록.db'),
                        WORK_BOARD_SPOOL=str(self.root / '기록 대기'))
        code = ("import json, runtime_paths as p; print(json.dumps([str(x) for x in "
                "(p.RESOURCE_ROOT,p.DATA_DIR,p.DB_PATH,p.SPOOL_DIR)]))")
        run = subprocess.run([sys.executable, '-c', code], env=self.env, cwd=ROOT,
                             capture_output=True, encoding='utf-8', timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [str(ROOT), self.env['HUGINN_DATA_DIR'],
                         self.env['WORK_BOARD_DB'], self.env['WORK_BOARD_SPOOL']])

    def test_frozen_paths_use_platform_user_folder(self):
        code = ("import sys,json,tempfile; sys.frozen=True; sys._MEIPASS=sys.argv[1]; "
                "sys.platform=sys.argv[2]; import runtime_paths as p; "
                "print(json.dumps([str(x) for x in (p.RESOURCE_ROOT,p.DATA_DIR,p.DB_PATH,p.SPOOL_DIR)]))")
        resources = self.root / '읽기 전용 앱'
        local_app_data = self.home / 'Local Apps'
        self.env['LOCALAPPDATA'] = str(local_app_data)
        for platform, data in [('darwin', self.home / 'Library/Application Support/Huginn'),
                               ('win32', local_app_data / 'Huginn')]:
            with self.subTest(platform=platform):
                run = subprocess.run([sys.executable, '-c', code, str(resources), platform],
                                     env=self.env, cwd=ROOT, capture_output=True, encoding='utf-8', timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout),
                                 list(map(str, [resources, data, data / 'board.db', data / 'spool'])))

    def test_serve_binds_loopback_and_reports_identity(self):
        data = self.root / '보드 기록'
        doc = self.home / '내 산출물/example.html'
        doc.parent.mkdir(parents=True)
        doc.write_text('<p>test document</p>', encoding='utf-8')
        settings=data/'.actions';settings.mkdir(parents=True)
        (settings/'connections.json').write_text(json.dumps({'documents':{'enabled':True,'root':str(doc.parent)}}),encoding='utf-8')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([*SERVICE, 'serve', '--port', str(port), '--data-dir', str(data),
                                    '--instance', 'test-instance', '--parent-pid', str(os.getpid())],
                                   env=self.env, cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with contextlib.closing(http.client.HTTPConnection('127.0.0.1', port, timeout=.2)) as conn:
                        conn.request('GET', '/api/health')
                        reply = conn.getresponse()
                        body = json.loads(reply.read())
                    break
                except OSError:
                    time.sleep(.03)
            else:
                self.fail('instance did not serve health')
            self.assertEqual(reply.status, 200)
            self.assertEqual(body, {'ok': True, 'app': 'Huginn', 'api': 1,
                                    'version': json.loads((ROOT / 'release.json').read_bytes())['version'],
                                    'instance': 'test-instance'})
            with contextlib.closing(http.client.HTTPConnection('127.0.0.1', port, timeout=1)) as conn:
                conn.request('GET', '/api/health', headers={'Origin': 'https://foreign.example'})
                self.assertEqual(conn.getresponse().status, 403)
            with contextlib.closing(http.client.HTTPConnection('127.0.0.1', port, timeout=1)) as conn:
                conn.request('GET', '/api/sessions')
                reply = conn.getresponse()
                self.assertEqual(reply.status, 200)
                self.assertEqual(json.loads(reply.read())['sessions'], [])
            with contextlib.closing(http.client.HTTPConnection('127.0.0.1', port, timeout=1)) as conn:
                conn.request('GET', '/doc/example.html')
                reply = conn.getresponse()
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.getheader('Content-Security-Policy'), 'sandbox allow-scripts allow-forms allow-popups')
                self.assertIn(b'test document', reply.read())
            self.assertTrue((data / 'board.db').is_file())
        finally:
            if process.poll() is None:
                process.terminate()
            _, errors = process.communicate(timeout=5)
            if process.returncode == 2:
                self.fail(errors.decode('utf-8', errors='replace'))

    def test_serve_exits_when_its_parent_exits(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        data = self.root / 'orphan-data'
        helper = (
            'import json,os,subprocess,sys; '
            'child=subprocess.Popen(json.loads(sys.argv[1])+["--parent-pid",str(os.getpid())],'
            'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
            'print(child.pid,flush=True); sys.stdin.read(1)'
        )
        child = [*SERVICE, 'serve', '--port', str(port), '--data-dir', str(data), '--instance', 'parent-test']
        parent = subprocess.Popen([sys.executable, '-c', helper, json.dumps(child)], env=self.env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            pid = int(parent.stdout.readline())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with contextlib.closing(http.client.HTTPConnection('127.0.0.1', port, timeout=.2)) as conn:
                        conn.request('GET', '/api/health')
                        self.assertEqual(json.loads(conn.getresponse().read())['instance'], 'parent-test')
                    break
                except OSError:
                    time.sleep(.03)
            else:
                self.fail('service did not start under its parent')

            parent.communicate(b'\n', timeout=5)
            self.assertEqual(parent.returncode, 0)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.15):
                        pass
                except (ConnectionRefusedError, TimeoutError):
                    if sys.platform != 'win32':
                        return
                    # Windows can time out instead of immediately refusing a
                    # closed loopback port. Require the actual child to exit.
                    import psutil
                    if not psutil.pid_exists(pid):
                        return
                time.sleep(.05)
            # The only child spawned by this test is still listening. Clean it
            # up before reporting the failure, including on Windows.
            if sys.platform == 'win32':
                import psutil
                psutil.Process(pid).terminate()
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            self.fail('service listener remained after its parent exited')
        finally:
            if parent.poll() is None:
                parent.communicate(b'\n', timeout=5)

    def test_serve_does_not_take_over_an_existing_listener(self):
        data = self.root / 'unused-data'
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            failed = self.service('serve', '--port', str(occupied.getsockname()[1]),
                                  '--data-dir', str(data), '--instance', 'different',
                                  '--parent-pid', str(os.getpid()))
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(data.exists())

    def test_serve_rejects_an_unrelated_parent(self):
        data = self.root / 'unused-data'
        failed = self.service('serve', '--port', '1', '--data-dir', str(data),
                              '--instance', 'orphan', '--parent-pid', str(os.getpid() + 1))
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('부모 프로세스', failed.stderr)
        self.assertFalse(data.exists())

    def test_migration_includes_live_wal_and_only_persistent_settings(self):
        source = self.root / '원본 자료'
        source.mkdir()
        p = source / '.actions'
        p.mkdir()
        for name in ('favorites.json', 'connections.json', 'settings.json', 'launch-requests.json', 'connection-checks.json'):
            (p / name).write_text('{"display":"한글"}', encoding='utf-8')
        for name in ('deliveries', 'launchers'):
            (p / name).mkdir()
        data = self.root / '설치 자료'
        with contextlib.closing(sqlite3.connect(source / 'board.db')) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE records (name TEXT)')
            conn.execute("INSERT INTO records VALUES ('함께 복사')")
            conn.commit()
            self.assertTrue((source / 'board.db-wal').stat().st_size)
            moved = self.service('migrate', '--from', str(source), '--data-dir', str(data))
        self.assertEqual(moved.returncode, 0, moved.stderr)
        with contextlib.closing(sqlite3.connect(data / 'board.db')) as db:
            self.assertEqual(db.execute('SELECT name FROM records').fetchall(), [('함께 복사',)])
        self.assertEqual(sorted(x.name for x in (data / '.actions').iterdir()),
                         ['connections.json', 'favorites.json', 'settings.json'])
        self.assertEqual((data / '.actions/settings.json').read_text(encoding='utf-8'), '{"display":"한글"}')
        self.assertTrue((source / '.actions/launchers').is_dir())

    def test_migration_preserves_existing_destination(self):
        source = self.root / 'source'
        source.mkdir()
        with contextlib.closing(sqlite3.connect(source / 'board.db')) as db:
            db.execute('CREATE TABLE events (id INT)')
        data = self.root / 'destination'
        data.mkdir()
        sentinel = data / 'board.db'
        sentinel.write_bytes(b'existing')
        moved = self.service('migrate', '--from', str(source), '--data-dir', str(data))
        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b'existing')

    def default_folder(self, product):
        if sys.platform == 'darwin':
            return self.home / 'Library/Application Support' / product
        if sys.platform == 'win32':
            return self.home / 'Local' / product
        return self.home / 'data' / product

    def test_default_folder_adopts_legacy_board_once_and_keeps_the_original(self):
        self.env.update(LOCALAPPDATA=str(self.home / 'Local'), XDG_DATA_HOME=str(self.home / 'data'))
        legacy, data = self.default_folder('Forkrail'), self.default_folder('Huginn')
        (legacy / '.actions').mkdir(parents=True)
        (legacy / 'spool').mkdir()
        with contextlib.closing(sqlite3.connect(legacy / 'board.db')) as db:
            db.execute('CREATE TABLE kept (name TEXT)')
            db.execute("INSERT INTO kept VALUES ('이전 기록')")
            db.commit()
        (legacy / '.actions/connections.json').write_text('{"summary":{"enabled":false}}', encoding='utf-8')
        (legacy / '.actions/connection-checks.json').write_text('{}', encoding='utf-8')
        (legacy / 'work_titles.json').write_text('{"제목":"요약"}', encoding='utf-8')
        event = {'kind': 'story', 'story': 'MOVE-1', 'title': '전송 대기 기록'}
        (legacy / 'spool/pending.jsonl').write_text(json.dumps(event) + '\n', encoding='utf-8')
        (data / 'spool').mkdir(parents=True)
        original = (legacy / 'board.db').read_bytes()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([*SERVICE, 'serve', '--port', str(port), '--data-dir', str(data),
                                    '--instance', 'adopt', '--parent-pid', str(os.getpid())],
                                   env=self.env, cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline, stories = time.monotonic() + 8, []
            while time.monotonic() < deadline and process.poll() is None and not stories:
                try:
                    with contextlib.closing(http.client.HTTPConnection('127.0.0.1', port, timeout=.3)) as conn:
                        conn.request('GET', '/api/state?story=MOVE-1')
                        stories = [s for s in json.loads(conn.getresponse().read())['stories'] if s.get('story') == 'MOVE-1']
                except (OSError, ValueError, KeyError):
                    pass
                time.sleep(.05)
            self.assertEqual([s.get('title') for s in stories], ['전송 대기 기록'])
        finally:
            if process.poll() is None:
                process.terminate()
            _, errors = process.communicate(timeout=5)
        with contextlib.closing(sqlite3.connect(data / 'board.db')) as db:
            self.assertEqual(db.execute('SELECT name FROM kept').fetchall(), [('이전 기록',)])
        self.assertEqual(sorted(x.name for x in (data / '.actions').iterdir() if not x.name.startswith('.')),
                         ['connections.json'])
        self.assertEqual((data / 'work_titles.json').read_text(encoding='utf-8'), '{"제목":"요약"}')
        self.assertEqual((legacy / 'board.db').read_bytes(), original)
        self.assertTrue((legacy / 'spool/pending.jsonl').is_file())

        import desktop_service
        with contextlib.closing(sqlite3.connect(legacy / 'board.db')) as db:
            db.execute("INSERT INTO kept VALUES ('이후 기록')")
            db.commit()
        self.assertFalse(desktop_service.adopt_legacy_data(data, legacy))
        with contextlib.closing(sqlite3.connect(data / 'board.db')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM kept').fetchone(), (1,))

    def test_explicit_folder_does_not_adopt_legacy_board(self):
        self.env.update(LOCALAPPDATA=str(self.home / 'Local'), XDG_DATA_HOME=str(self.home / 'data'))
        legacy = self.default_folder('Forkrail')
        legacy.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(legacy / 'board.db')) as db:
            db.execute('CREATE TABLE kept (name TEXT)')
        data = self.root / '지정 폴더'
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([*SERVICE, 'serve', '--port', str(port), '--data-dir', str(data),
                                    '--instance', 'explicit', '--parent-pid', str(os.getpid())],
                                   env=self.env, cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (data / 'board.db').exists():
                time.sleep(.05)
        finally:
            process.terminate()
            process.communicate(timeout=5)
        with contextlib.closing(sqlite3.connect(data / 'board.db')) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='kept'").fetchall())

    def test_service_self_test_ignores_explicit_database_and_spool(self):
        db = self.root / 'existing.db'
        db.write_bytes(b'preserve')
        self.env['WORK_BOARD_DB'] = str(db)
        spool = self.root / 'live spool'
        spool.mkdir()
        pending = spool / 'should-remain.jsonl'
        pending.write_text('{"kind":"log"}\n', encoding='utf-8')
        self.env['WORK_BOARD_SPOOL'] = str(spool)
        tested = self.service('self-test')
        self.assertEqual(tested.returncode, 0, tested.stderr)
        self.assertEqual(db.read_bytes(), b'preserve')
        self.assertTrue(pending.is_file())

    def test_bundled_workboard_actions_use_data_directory(self):
        data = self.root / '작업 보드'
        self.env['HUGINN_DATA_DIR'] = str(data)
        run = self.service('wb', 'actions', 'configure', '--auto-terminal', 'off')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(json.loads((data / '.actions/settings.json').read_bytes())['auto_terminal'])
