"""Entry point for the bundled Huginn service and local CLI tools."""
import argparse
from contextlib import closing
import ctypes
import errno
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading


DURABLE_SETTINGS = ('settings.json', 'favorites.json', 'connections.json')
LEGACY_CACHES = ('work_titles.json', 'story_cache.json')
RENAME_EXCL = 0x00000004


def publish_migration(staged, destination):
    # Both released platforms can atomically rename without replacing an
    # existing directory, including one created after the initial check.
    if sys.platform == 'win32':
        staged.rename(destination)
        return
    if sys.platform == 'darwin':
        rename_exclusive = ctypes.CDLL(None, use_errno=True).renamex_np
        rename_exclusive.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename_exclusive.restype = ctypes.c_int
        if rename_exclusive(os.fsencode(staged), os.fsencode(destination), RENAME_EXCL):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), destination)
        return

    # Keep source-mode migration usable on other POSIX systems too. Reserve
    # the destination exclusively, then publish the already-prepared files.
    destination.mkdir(mode=0o700)
    try:
        (staged / '.actions').rename(destination / '.actions')
        (staged / 'board.db').rename(destination / 'board.db')
    except OSError:
        settings = destination / '.actions'
        if settings.is_dir():
            for name in DURABLE_SETTINGS:
                (settings / name).unlink(missing_ok=True)
            settings.rmdir()
        destination.rmdir()
        raise


def migrate(source, destination):
    source = source.expanduser().resolve(strict=True)
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(errno.EEXIST, '대상 폴더가 이미 있습니다.', destination)
    destination = destination.resolve()
    if not source.is_dir() or destination.is_relative_to(source):
        raise ValueError('설치 자료 폴더는 원본과 별도의 새 폴더여야 합니다.')
    database = source / 'board.db'
    if not database.is_file() or database.is_symlink():
        raise ValueError('원본 폴더에서 board.db를 찾을 수 없습니다.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.huginn-migrate-', dir=destination.parent) as folder:
        staged = Path(folder) / 'data'
        staged.mkdir(mode=0o700)
        settings = staged / '.actions'
        settings.mkdir(mode=0o700)
        for name in DURABLE_SETTINGS:
            path = source / '.actions' / name
            if path.is_file() and not path.is_symlink():
                with path.open('rb') as infile, (settings / name).open('xb') as outfile:
                    shutil.copyfileobj(infile, outfile)
                if os.name != 'nt':
                    (settings / name).chmod(0o600)
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as current:
            with closing(sqlite3.connect(staged / 'board.db')) as copied:
                current.backup(copied)
                copied.execute('PRAGMA journal_mode=DELETE')
        if os.name != 'nt':
            (staged / 'board.db').chmod(0o600)
        publish_migration(staged, destination)


def adopt_legacy_data(data_dir, legacy):
    data_dir = data_dir.expanduser().absolute()
    if (data_dir / 'board.db').exists() or not (legacy / 'board.db').is_file() or (legacy / 'board.db').is_symlink():
        return False
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.huginn-adopt-', dir=data_dir.parent) as folder:
        staged = Path(folder) / 'data'
        migrate(legacy, staged)
        for name in LEGACY_CACHES:
            path = legacy / name
            if path.is_file() and not path.is_symlink():
                shutil.copyfile(path, staged / name)
        data_dir.mkdir(mode=0o700, exist_ok=True)
        for entry in staged.iterdir():
            target = data_dir / entry.name
            if not target.exists() and not target.is_symlink():
                entry.rename(target)
    pending = legacy / 'spool'
    if pending.is_dir() and not pending.is_symlink():
        spool = data_dir / 'spool'
        spool.mkdir(mode=0o700, exist_ok=True)
        for path in pending.iterdir():
            if path.is_file() and not path.is_symlink() and not (spool / path.name).exists():
                shutil.copyfile(path, spool / path.name)
    return True


def configure_service(port, data_dir, instance):
    os.environ['WORK_BOARD_PORT'] = str(port)
    os.environ['HUGINN_DATA_DIR'] = str(data_dir.expanduser().resolve())
    os.environ['HUGINN_INSTANCE'] = instance


def serve(args):
    from platform_support import parent_monitor

    parent_alive = parent_monitor(args.parent_pid)
    configure_service(args.port, args.data_dir, args.instance)
    from runtime_paths import DATA_DIR, LEGACY_PRODUCT, user_data_dir
    if DATA_DIR == user_data_dir().resolve():
        adopt_legacy_data(DATA_DIR, user_data_dir(LEGACY_PRODUCT))
    import server

    with server.create_server() as httpd:
        stop = threading.Event()

        def monitor():
            while not stop.wait(.5):
                if not parent_alive():
                    httpd.shutdown()
                    return

        threading.Thread(target=monitor, name='parent-monitor', daemon=True).start()
        threading.Thread(target=server.drain_spool, name='spool', daemon=True).start()
        try:
            httpd.serve_forever(poll_interval=.25)
        finally:
            stop.set()


def self_test():
    with tempfile.TemporaryDirectory(prefix='huginn-self-test-') as folder:
        root = Path(folder)
        home = root / '테스트 홈'
        home.mkdir()
        for key in ('WORK_BOARD_DB', 'WORK_BOARD_SPOOL', 'WORK_BOARD_URL'):
            os.environ.pop(key, None)
        os.environ.update(HOME=str(home), USERPROFILE=str(home))
        data = root / '보드 자료'
        instance = secrets.token_hex(16)
        settings = data / '.actions'
        settings.mkdir(parents=True)
        (settings / 'connections.json').write_text('{"summary":{"enabled":false}}', encoding='utf-8')
        configure_service(0, data, instance)
        import server

        with server.create_server() as httpd:
            worker = threading.Thread(target=httpd.serve_forever, daemon=True)
            worker.start()
            try:
                check_service(httpd.server_port, instance)
            finally:
                httpd.shutdown()
                worker.join()
        if not (data / 'board.db').is_file():
            raise ValueError('서비스 점검에서 사용자 폴더의 DB를 찾지 못했습니다.')


def check_service(port, instance):
    def request(path, method='GET', data=None, headers=None, status=200):
        with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=5)) as conn:
            conn.request(method, path, body=data, headers=headers or {})
            response = conn.getresponse()
            body = response.read()
            if response.status != status:
                raise ValueError(f'서비스 점검 {path} HTTP {response.status}, 기대 {status}')
            return body

    health = json.loads(request('/api/health'))
    if (health.get('ok') is not True or health.get('app') != 'Huginn' or health.get('api') != 1
            or not health.get('version') or health.get('instance') != instance):
        raise ValueError('서비스 점검에서 올바른 제품 정보를 확인하지 못했습니다.')
    request('/api/health', headers={'Origin': 'https://foreign.example'}, status=403)
    if b'<html' not in request('/').lower() or not request('/assets/brand.png').startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('서비스의 화면 또는 아이콘 리소스를 읽지 못했습니다.')
    if json.loads(request('/api/sessions')).get('sessions') != []:
        raise ValueError('서비스 점검의 세션 경로가 격리되지 않았습니다.')
    request('/api/actions', method='POST', headers={'Origin': f'http://127.0.0.1:{port}'}, status=403)
    event = json.dumps({'kind': 'story', 'story': 'TEST-1', 'title': '서비스 점검'}).encode('utf-8')
    request('/api/events', method='POST', data=event, headers={'Content-Type': 'application/json'})
    state = json.loads(request('/api/state?story=TEST-1'))
    if not any(row.get('story') == 'TEST-1' and row.get('title') == '서비스 점검' for row in state['stories']):
        raise ValueError('서비스 점검에서 기록을 읽지 못했습니다.')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.name == 'nt':
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(encoding='utf-8')
    if argv[:1] == ['wb']:
        import wbcore
        return wbcore.main(argv[1:])
    if argv == ['hook']:
        import claude_hook
        return claude_hook.main()

    parser = argparse.ArgumentParser(prog='huginn-service')
    commands = parser.add_subparsers(dest='command', required=True)
    service = commands.add_parser('serve')
    service.add_argument('--port', required=True, type=int)
    service.add_argument('--data-dir', required=True, type=Path)
    service.add_argument('--instance', required=True)
    service.add_argument('--parent-pid', required=True, type=int)
    commands.add_parser('self-test')
    commands.add_parser('wb', help='run a Huginn command')
    commands.add_parser('hook', help='read a Claude hook event from standard input')
    migration = commands.add_parser('migrate')
    migration.add_argument('--from', required=True, type=Path, dest='source')
    migration.add_argument('--data-dir', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            if not 1 <= args.port <= 65535 or not args.instance:
                service.error('port and instance must identify this local service')
            serve(args)
        elif args.command == 'self-test':
            self_test()
            print('Huginn service self-test passed')
        elif args.command == 'migrate':
            migrate(args.source, args.data_dir)
        else:
            parser.error('hook does not take additional arguments')
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'Huginn: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
