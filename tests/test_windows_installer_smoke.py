import importlib
from pathlib import Path, PureWindowsPath
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
with patch.object(sys, 'path', [str(SCRIPTS), *sys.path]):
    smoke = importlib.import_module('smoke_release')


class NSISCommandTests(unittest.TestCase):
    def test_install_destination_is_last_and_unquoted(self):
        setup = PureWindowsPath(r'C:\Program Files\Huginn\Installer.exe')
        target = PureWindowsPath(r'C:\Users\runner\한글 폴더\Huginn')
        self.assertEqual(
            smoke.nsis_command(setup, '/D', target),
            '"C:\\Program Files\\Huginn\\Installer.exe" /S /D=C:\\Users\\runner\\한글 폴더\\Huginn',
        )

    def test_in_app_update_runs_visible_progress_mode(self):
        setup = PureWindowsPath(r'C:\Temp\Huginn-update-1\Huginn-Setup.exe')
        self.assertEqual(
            smoke.nsis_command(setup, flags=('/UPDATE', '/RELAUNCH'), silent=False),
            'C:\\Temp\\Huginn-update-1\\Huginn-Setup.exe /UPDATE /RELAUNCH',
        )

    def test_installer_skips_welcome_and_finish_pages_for_updates(self):
        script = (SCRIPTS.parent / 'packaging/windows/installer.nsi').read_text(encoding='utf-8')
        pages = script[script.index('MUI_PAGE_CUSTOMFUNCTION_PRE'):script.index('MUI_UNPAGE_CONFIRM')]
        self.assertEqual(pages.count('MUI_PAGE_CUSTOMFUNCTION_PRE SkipPageForUpdate'), 2)
        self.assertLess(pages.index('MUI_PAGE_CUSTOMFUNCTION_PRE SkipPageForUpdate'), pages.index('MUI_PAGE_WELCOME'))
        self.assertLess(pages.index('MUI_PAGE_INSTFILES'), pages.rindex('MUI_PAGE_CUSTOMFUNCTION_PRE SkipPageForUpdate'))
        self.assertIn('${GetOptions} $R0 "/UPDATE" $R1', script)
        updater = (SCRIPTS.parent / 'desktop/updates_windows.go').read_text(encoding='utf-8')
        self.assertIn('[]string{"/UPDATE", "/RELAUNCH"}', updater)

    def test_copied_uninstaller_receives_last_unquoted_install_path(self):
        uninstall = PureWindowsPath(r'C:\Temp\Copied Uninstaller.exe')
        target = PureWindowsPath(r'C:\Users\runner\한글 폴더\Huginn')
        self.assertEqual(
            smoke.nsis_command(uninstall, '_?', target),
            '"C:\\Temp\\Copied Uninstaller.exe" /S _?=C:\\Users\\runner\\한글 폴더\\Huginn',
        )
