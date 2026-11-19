"""Create a read-only drag-install DMG without launching Finder or the app."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = '.background/background.tiff'
WINDOW_BOUNDS = '{{160, 160}, {720, 440}}'
ICON_LOCATIONS = {'Huginn.app': (196, 244), 'Applications': (524, 244)}


def write_finder_layout(mount: Path) -> None:
    # Lazy imports keep release tooling importable on non-macOS hosts.
    from ds_store import DSStore
    from mac_alias import Alias

    # statfs returns canonical mount paths (e.g. /private/var, not /var).
    # Match that form so mac_alias computes a path relative to this volume.
    alias = Alias.for_file(str((mount / BACKGROUND).resolve()))
    with DSStore.open(str(mount / '.DS_Store'), 'w+') as store:
        store['.']['vSrn'] = ('long', 1)
        store['.']['icvl'] = ('type', b'icnv')
        store['.']['bwsp'] = {
            'WindowBounds': WINDOW_BOUNDS, 'ShowStatusBar': False, 'ShowToolbar': False,
            'ShowPathbar': False, 'ShowSidebar': False, 'ContainerShowSidebar': False,
            'PreviewPaneVisibility': False, 'ShowTabView': False, 'SidebarWidth': 0,
        }
        store['.']['icvp'] = {
            'viewOptionsVersion': 1, 'backgroundType': 2,
            'backgroundImageAlias': alias.to_bytes(),
            'backgroundColorRed': 244 / 255, 'backgroundColorGreen': 244 / 255,
            'backgroundColorBlue': 240 / 255, 'gridOffsetX': 0.0, 'gridOffsetY': 0.0,
            'gridSpacing': 100.0, 'arrangeBy': 'none', 'showIconPreview': True,
            'showItemInfo': False, 'labelOnBottom': True, 'textSize': 14.0, 'iconSize': 128.0,
        }
        for name, position in ICON_LOCATIONS.items():
            store[name]['Iloc'] = position


def _finder_info(mount: Path) -> bytes:
    # Python's os xattr API is Linux-only; use the macOS system utility.
    attributes = subprocess.check_output(['xattr', str(mount)], text=True).splitlines()
    if 'com.apple.FinderInfo' not in attributes:
        return bytes(32)
    value = subprocess.check_output(['xattr', '-px', 'com.apple.FinderInfo', str(mount)], text=True)
    return bytes.fromhex(value)


def _set_volume_icon(mount: Path) -> None:
    info = bytearray(_finder_info(mount))
    flags = int.from_bytes(info[8:10], 'big') | 0x0400  # kHasCustomIcon
    info[8:10] = flags.to_bytes(2, 'big')
    subprocess.run(['xattr', '-wx', 'com.apple.FinderInfo', info.hex(), str(mount)], check=True)


def verify_layout(mount: Path) -> dict:
    """Validate a mounted image without writing to it; return JSON-safe evidence."""
    from ds_store import DSStore
    from mac_alias import Alias
    from PIL import Image

    mount = Path(mount)
    if not (mount / 'Huginn.app/Contents/Info.plist').is_file():
        raise ValueError('DMG is missing Huginn.app')
    applications = mount / 'Applications'
    if not applications.is_symlink() or os.readlink(applications) != '/Applications':
        raise ValueError('DMG Applications must be a symlink to /Applications')
    with (mount / '.VolumeIcon.icns').open('rb') as icon:
        if icon.read(4) != b'icns':
            raise ValueError('DMG volume icon is not an ICNS file')
    flags = int.from_bytes(_finder_info(mount)[8:10], 'big')
    if not flags & 0x0400:
        raise ValueError('DMG volume icon is not enabled')
    with Image.open(mount / BACKGROUND) as background:
        sizes = []
        for index in range(background.n_frames):
            background.seek(index)
            sizes.append(list(background.size))
        if sizes != [[720, 440], [1440, 880]]:
            raise ValueError('DMG background must include 1x and 2x representations')
    with DSStore.open(str(mount / '.DS_Store'), 'r') as store:
        window, view = store['.']['bwsp'], store['.']['icvp']
        if window['WindowBounds'] != WINDOW_BOUNDS:
            raise ValueError('DMG Finder window size is incorrect')
        if any(window.get(key) for key in ('ShowToolbar', 'ShowStatusBar', 'ShowPathbar', 'ShowSidebar')):
            raise ValueError('DMG Finder chrome must be hidden')
        if store['.']['icvl'] != (b'type', b'icnv'):
            raise ValueError('DMG must open in icon view')
        if view['backgroundType'] != 2 or view['iconSize'] != 128.0 or view['arrangeBy'] != 'none':
            raise ValueError('DMG Finder icon layout is incorrect')
        for name, position in ICON_LOCATIONS.items():
            if store[name]['Iloc'] != position:
                raise ValueError('DMG Finder icon position is incorrect: ' + name)
        alias = Alias.from_bytes(view['backgroundImageAlias'])
        if alias.volume.name != 'Huginn' or alias.target.posix_path.lstrip('/') != BACKGROUND:
            raise ValueError('DMG background alias must point inside the Huginn volume')
    visible = sorted(path.name for path in mount.iterdir() if not path.name.startswith('.'))
    if visible != ['Applications', 'Huginn.app']:
        raise ValueError('DMG contains unexpected visible files')
    return {'window_size': [720, 440], 'icon_size': 128,
            'icon_locations': {name: list(position) for name, position in ICON_LOCATIONS.items()},
            'background': BACKGROUND, 'background_sizes': sizes,
            'applications_target': '/Applications', 'volume_icon': True, 'visible_entries': visible}


def create_dmg(app: Path, output: Path, work: Path, volume_icon: Path) -> None:
    """Package an already signed/stapled app; app contents and signature are preserved."""
    if sys.platform != 'darwin':
        raise ValueError('DMG creation requires macOS')
    # Fail before creating/copying files if build dependencies are missing.
    import ds_store  # noqa: F401
    import mac_alias  # noqa: F401

    app, output, work, volume_icon = map(Path, (app, output, work, volume_icon))
    if app.is_symlink() or not (app / 'Contents/Info.plist').is_file():
        raise ValueError('DMG source must be an application bundle')
    if not volume_icon.is_file():
        raise FileNotFoundError(volume_icon)
    if output.exists() or output.is_symlink():
        raise ValueError('DMG output already exists: ' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='dmg-', dir=work))
    mount = scratch / 'mount'
    attached = False

    def run(*args, **kwargs):
        return subprocess.run([str(arg) for arg in args], check=True, **kwargs)

    try:
        stage = scratch / 'stage'
        stage.mkdir()
        run('ditto', '--rsrc', '--extattr', app, stage / 'Huginn.app')
        (stage / 'Applications').symlink_to('/Applications')
        shutil.copy2(volume_icon, stage / '.VolumeIcon.icns')
        run(sys.executable, ROOT / 'packaging/macos/render_background.py', '--output', scratch / 'artwork')
        (stage / '.background').mkdir()
        shutil.copy2(scratch / 'artwork/background.tiff', stage / BACKGROUND)
        (stage / '.metadata_never_index').touch()
        # Leave room for Finder metadata, including tiny fixture bundles used in tests.
        size = sum(path.stat().st_size for path in stage.rglob('*') if path.is_file() and not path.is_symlink())
        size_mb = max(64, (size * 12 // 10) // (1024 * 1024) + 32)
        writable = scratch / 'writable.dmg'
        run('hdiutil', 'create', '-quiet', '-size', f'{size_mb}m', '-fs', 'HFS+',
            '-volname', 'Huginn', '-srcfolder', stage, '-format', 'UDRW', writable)
        mount.mkdir()
        run('hdiutil', 'attach', '-quiet', '-nobrowse', '-noautoopen', '-mountpoint', mount, writable)
        attached = True
        try:
            write_finder_layout(mount)
            _set_volume_icon(mount)
            verify_layout(mount)
        finally:
            run('hdiutil', 'detach', '-quiet', mount)
            attached = False
        run('hdiutil', 'convert', '-quiet', writable, '-format', 'UDZO',
            '-imagekey', 'zlib-level=9', '-o', output)
        run('hdiutil', 'verify', '-quiet', output)
    finally:
        # Never recursively remove a scratch directory while it still holds a mount.
        if not attached:
            shutil.rmtree(scratch)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--volume-icon', type=Path, required=True)
    args = parser.parse_args()
    create_dmg(args.app, args.output, args.work, args.volume_icon)
