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

    def test_copied_uninstaller_receives_last_unquoted_install_path(self):
        uninstall = PureWindowsPath(r'C:\Temp\Copied Uninstaller.exe')
        target = PureWindowsPath(r'C:\Users\runner\한글 폴더\Huginn')
        self.assertEqual(
            smoke.nsis_command(uninstall, '_?', target),
            '"C:\\Temp\\Copied Uninstaller.exe" /S _?=C:\\Users\\runner\\한글 폴더\\Huginn',
        )
