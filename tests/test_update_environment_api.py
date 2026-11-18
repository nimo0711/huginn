"""The browser uses service capabilities, never its own user-agent OS."""
from email.message import Message
from unittest.mock import Mock, patch
import unittest

import server


class UpdateEnvironmentApiTests(unittest.TestCase):
    def catalog(self, platform, release=None):
        handler = object.__new__(server.Handler)
        handler.path = '/api/actions'
        handler.headers = Message()
        handler.headers['Host'] = f'127.0.0.1:{server.PORT}'
        handler.headers['User-Agent'] = 'Windows browser on a different computer'
        handler._json = lambda code, value: setattr(handler, 'result', (code, value))
        actions = Mock(home='/tmp')
        actions.settings.return_value = {'auto_servers': True, 'auto_terminal': True, 'launch_app': ''}
        actions.catalog.return_value = actions.shortcuts.return_value = actions.connections.catalog.return_value = []
        actions.guide.return_value = ''
        actions.delivery.describe.return_value = actions.delivery.lookup.return_value = None
        with patch.object(server, 'ACTIONS', actions), patch.object(server.scan, 'all_sessions', return_value=[]), \
                patch('sys.platform', platform), patch.object(server, 'RELEASE', release or {'version': '1.2.0-rc.1', 'repository': 'nimo0711/forkrail'}):
            handler.do_GET()
        self.assertEqual(handler.result[0], 200)
        return handler.result[1]

    def test_api_reports_actual_os_and_supported_settings(self):
        for platform, expected in [
            ('darwin', {'os': 'macos', 'terminal_app_configurable': True, 'terminal_focus_supported': True, 'shell_label': '터미널'}),
            ('win32', {'os': 'windows', 'terminal_app_configurable': False, 'terminal_focus_supported': False, 'shell_label': 'PowerShell'}),
            ('linux', {'os': 'other', 'terminal_app_configurable': False, 'terminal_focus_supported': False, 'shell_label': '터미널'}),
        ]:
            with self.subTest(platform=platform):
                self.assertEqual(self.catalog(platform).get('environment'), expected)

    def test_release_link_comes_from_packaged_repository_not_request_input(self):
        result = self.catalog('darwin')
        self.assertEqual(result.get('app'), {'version': '1.2.0-rc.1', 'release_url': 'https://github.com/nimo0711/forkrail/releases'})
        for repository in ['https://evil.test/repo', 'owner/repo?redirect=evil', '../repo', 'owner/repo/../../evil']:
            with self.subTest(repository=repository):
                app = self.catalog('darwin', {'version': '1.2.0', 'repository': repository}).get('app', {})
                self.assertEqual(app.get('release_url'), '')
