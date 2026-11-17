import json
from pathlib import Path
import shlex
import tempfile
import unittest

import install_claude_hooks
import scan
from scripts.service_plist import configuration


class PackagingTests(unittest.TestCase):
    def test_hooks_preserve_existing_settings_and_do_not_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Path(folder)/'settings.json'
            existing={'model':'existing-model','hooks':{'Stop':[{'hooks':[{'type':'command','command':'existing-command'}]}]}}
            settings.write_text(json.dumps(existing))
            command=shlex.join(['/test python/bin/python3','/test board/claude_hook.py'])
            first=install_claude_hooks.install(settings,command)
            second=install_claude_hooks.install(settings,command)
            result=json.loads(settings.read_text())
            self.assertEqual(first,second)
            self.assertTrue(all(count==1 for count in second.values()))
            self.assertEqual(result['model'],'existing-model')
            self.assertEqual(result['hooks']['Stop'][0],existing['hooks']['Stop'][0])

    def test_hooks_can_create_a_new_settings_file(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=Path(folder)/'new/settings.json'
            self.assertTrue(all(count==1 for count in install_claude_hooks.install(settings,'test-hook').values()))

    def test_generated_service_keeps_paths_with_spaces_as_separate_arguments(self):
        repo=Path('/test user/My Board');python=Path('/test user/Python/bin/python3')
        value=configuration(repo,python)
        self.assertEqual(value['ProgramArguments'],[str(python),str(repo/'server.py')])
        self.assertEqual(value['WorkingDirectory'],str(repo))

    def test_scanner_uses_current_home_and_supports_home_alias(self):
        path=scan.HOME+'/projects/demo/main.py'
        self.assertEqual(scan.PATH_RE.findall('읽기 '+path),[path])
        self.assertEqual(scan.PATH_RE.findall('읽기 ~/projects/demo/main.py'),['~/projects/demo/main.py'])
