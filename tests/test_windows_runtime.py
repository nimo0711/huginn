import contextlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import board_actions
import local_runtime
import scan
from session_delivery import SessionDelivery


class WindowsRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'requires Windows file locking')
    def test_lock_excludes_another_process(self):
        from platform_support import file_lock

        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / '상태 파일'
            code = ('import sys; from pathlib import Path; from platform_support import file_lock; '
                    'p=Path(sys.argv[1]); flag=Path(sys.argv[2]); ready=Path(sys.argv[3]); '
                    'ready.touch();\nwith file_lock(p): flag.touch()')
            flag, ready = Path(folder) / 'entered', Path(folder) / 'waiting'
            with file_lock(lock):
                child = subprocess.Popen([sys.executable, '-c', code, str(lock), str(flag), str(ready)])
                self.addCleanup(child.wait, timeout=6)
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.wait(timeout=.3)
                self.assertFalse(flag.exists())
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertTrue(flag.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'requires native Windows argv parsing in PowerShell')
    def test_terminal_launcher_preserves_multiline_unicode_and_shell_characters(self):
        from platform_support import powershell_command, write_powershell_launcher

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / '작업 폴더'
            directory.mkdir()
            target = directory / '결과.json'
            payload = directory / '요청.json'
            prompt = '한글 "인용" $PROFILE & | %TEST% ! " &\n두 번째 줄 \\'
            code = 'import json,sys,os;open(sys.argv[1],"w",encoding="utf-8").write(json.dumps([os.getcwd(),sys.argv[2:],os.getenv("CLAUDECODE")]))'
            args = [sys.executable, '-c', code, str(target), '', prompt, 'C:\\경로 이름\\끝\\']
            write_powershell_launcher(payload, args, directory, remove_agent_context=True)
            run = subprocess.run(powershell_command(payload, stay_open=False), env={**os.environ, 'CLAUDECODE': 'parent'},
                                 capture_output=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(target.read_bytes()), [str(directory), ['', prompt, args[-1]], None])

    @unittest.skipUnless(sys.platform == 'win32', 'requires Windows PowerShell')
    def test_board_launches_codex_with_one_literal_prompt_argument(self):
        from platform_support import powershell_command

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / '작업 폴더'
            project.mkdir()
            skill = project / '.codex/skills/report/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: report\n---\nReport skill', encoding='utf-8')
            tool = root / '도구.py'
            tool.write_text('import json, os, sys\nfrom pathlib import Path\n'
                            'Path(os.environ["WB_TEST_ARGS"]).write_text(json.dumps(sys.argv[1:]))\n',
                            encoding='utf-8')
            result = root / '결과.json'
            marker = root / 'unsafe'
            prompt = f'한글 $HOME | "&" ; New-Item -Path "{marker}"\nsecond line'
            action = board_actions.ActionStore(root / 'state', root)
            with patch.object(board_actions, 'agent_command', return_value=[sys.executable, str(tool)]), \
                    patch.object(board_actions, 'open_terminal'):
                request = action.launch(action.catalog(project)[0]['id'], project, 'windows-launch-test', prompt)
            run = subprocess.run(powershell_command(request['launcher'], stay_open=False),
                                 env={**os.environ, 'WB_TEST_ARGS': str(result)}, capture_output=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            args = json.loads(result.read_bytes())
            self.assertEqual(args[:2], ['-C', str(project.resolve())])
            self.assertEqual(len(args), 3)
            self.assertTrue(args[-1].endswith(prompt))
            self.assertFalse(marker.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'requires Windows psutil process discovery')
    def test_process_and_listener_detection_does_not_signal_processes(self):
        with patch.object(os, 'kill', side_effect=AssertionError('must not signal Windows processes')):
            self.assertTrue(scan._alive(os.getpid()))
            self.assertFalse(scan._alive(0))
            self.assertIn(os.getpid(), local_runtime.processes())
            with contextlib.closing(http.server.HTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)) as httpd:
                self.assertIn({'pid': os.getpid(), 'port': httpd.server_port, 'host': '127.0.0.1'},
                              local_runtime.listeners())

    def test_unsupported_direct_send_explains_reason_and_does_not_open_transport(self):
        with tempfile.TemporaryDirectory() as folder, patch('session_delivery.IS_WINDOWS', True, create=True):
            delivery = SessionDelivery(folder, folder)
            session = {'id': 'selected', 'agent': 'codex', 'cwd_path': folder}
            with patch.object(delivery, 'codex_rpc', side_effect=AssertionError('must not open a socket')):
                connection = delivery.describe(session)
                self.assertFalse(connection['available'])
                self.assertIn('Windows', connection['message'])
                with self.assertRaisesRegex(ValueError, 'Windows'):
                    delivery.send(session, {'agent': 'codex', 'id': 'skill', 'label': 'skill'}, 'valid-request')
            self.assertFalse((Path(folder) / 'deliveries').exists())
