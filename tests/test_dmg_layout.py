"""Exercise the Finder metadata and, optionally, a real mounted disk image."""
import importlib.util
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HAS_LAYOUT_DEPS = all(importlib.util.find_spec(name) for name in ('ds_store', 'mac_alias'))


@unittest.skipUnless(sys.platform == 'darwin' and HAS_LAYOUT_DEPS, 'macOS DMG build dependencies required')
class DmgLayoutTests(unittest.TestCase):
    def setUp(self):
        from scripts import create_dmg
        self.dmg = create_dmg
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_metadata_places_native_icons_without_automatic_rearrangement(self):
        from ds_store import DSStore
        # The native Alias read requires HFS+; the optional mounted test covers it.
        with mock.patch('mac_alias.Alias.for_file') as native_alias:
            native_alias.return_value.to_bytes.return_value = b'background-alias-record'
            self.dmg.write_finder_layout(self.folder)
        with DSStore.open(str(self.folder / '.DS_Store'), 'r') as store:
            self.assertEqual(store['Huginn.app']['Iloc'], (196, 244))
            self.assertEqual(store['Applications']['Iloc'], (524, 244))
            view = store['.']['icvp']
            self.assertEqual(view['backgroundType'], 2)
            self.assertEqual(view['iconSize'], 128.0)
            self.assertEqual(view['arrangeBy'], 'none')
            self.assertFalse(store['.']['bwsp']['ShowToolbar'])
            self.assertFalse(store['.']['bwsp']['ShowSidebar'])
            self.assertEqual(store['.']['bwsp']['WindowBounds'], '{{160, 160}, {720, 440}}')
            self.assertEqual(view['backgroundImageAlias'], b'background-alias-record')

    @unittest.skipUnless(os.environ.get('FORKRAIL_DMG_INTEGRATION') == '1', 'opt-in mounted DMG integration')
    def test_readonly_image_contains_a_copyable_app_and_portable_finder_layout(self):
        from PIL import Image
        app = self.folder / 'source/Huginn.app'
        executable = app / 'Contents/MacOS/Huginn'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture app; never executed')
        executable.chmod(0o755)
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleName': 'Huginn'}))
        icon = self.folder / 'volume.icns'
        Image.open(Path(__file__).resolve().parents[1] / 'app/assets/forkrail-icon.png').save(icon)
        output = self.folder / 'Huginn.dmg'
        self.dmg.create_dmg(app, output, self.folder / 'work', icon)
        details = plistlib.loads(subprocess.check_output(['hdiutil', 'imageinfo', '-plist', str(output)]))
        self.assertEqual(details['Format'], 'UDZO')
        mount = self.folder / 'read-only-mount'
        mount.mkdir()
        subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(output)], check=True, capture_output=True)
        try:
            result = self.dmg.verify_layout(mount)
            self.assertEqual(result['applications_target'], '/Applications')
            self.assertEqual(result['window_size'], [720, 440])
            self.assertEqual((mount / 'Huginn.app/Contents/MacOS/Huginn').read_bytes(), executable.read_bytes())
            self.assertTrue((mount / 'Huginn.app/Contents/MacOS/Huginn').stat().st_mode & 0o111)
            with self.assertRaises(OSError):
                (mount / 'should-be-readonly').write_text('cannot write')
        finally:
            subprocess.run(['hdiutil', 'detach', str(mount)], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
