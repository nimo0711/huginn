"""Exercise the built package using temporary user data and a real WebView."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager

from release_tools import artifact_stem, config, sha256

ROOT=Path(__file__).resolve().parents[1]


def run(args, **kwargs):subprocess.run(list(map(str,args)),check=True,timeout=120,**kwargs)


@contextmanager
def windows_file_in_use(path):
    """Real Windows sharing violation, created with a handle that denies write and delete sharing."""
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,1,None,3,0,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:yield
    finally:kernel.CloseHandle(handle)


def smoke(executable,env,folder):
    results={}
    for flag,mode in [('--smoke-test','service'),('--smoke-ui','ui')]:
        evidence=folder/(mode+'.json')
        evidence.unlink(missing_ok=True)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        current={**env,'HUGINN_SMOKE_RESULT':str(evidence),'WORK_BOARD_PORT':str(port)}
        run([executable,flag],env=current)
        data=json.loads(evidence.read_text(encoding='utf-8'))
        if data.get('ok') is not True or data.get('mode')!=mode:raise ValueError('Packaged '+mode+' smoke did not pass')
        # Its managed child must have ended; a responding port is a resource leak.
        with socket.socket() as check:
            check.settimeout(1)
            if check.connect_ex(('127.0.0.1',port))==0:raise ValueError('Service still listens after the app exited')
        results[mode]=data
    return results


def smoke_mac_app(app, bundle_id, env, folder):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != bundle_id:
        raise ValueError(f'Wrong bundle ID in {app}')
    executable = app / 'Contents/MacOS' / info['CFBundleExecutable']
    return smoke(executable, env, folder)


@contextmanager
def mounted_disk_image(image, folder):
    mount=folder/'mounted-dmg';mount.mkdir()
    run(['/usr/bin/hdiutil','verify',image])
    run(['/usr/bin/hdiutil','attach','-readonly','-nobrowse','-mountpoint',mount,image])
    try:
        yield mount
    finally:
        run(['/usr/bin/hdiutil','detach',mount])


def smoke_mac_disk_image(image, bundle_id, env, folder):
    from create_dmg import verify_layout
    installed=folder/'Applications/Huginn.app'
    installed.parent.mkdir()
    with mounted_disk_image(image,folder) as mount:
        layout=verify_layout(mount)
        source=mount/'Huginn.app'
        if source.is_symlink() or not source.is_dir():
            raise ValueError('DMG must contain the actual Huginn.app')
        # Finder drag installation copies the whole app bundle. Use the same
        # metadata-preserving copy without touching the developer's Applications.
        run(['/usr/bin/ditto',source,installed])
    run(['/usr/bin/codesign','--verify','--deep','--strict',installed])
    results=smoke_mac_app(installed,bundle_id,env,folder)
    return results,{'layout_verified':True,'copied':True,'layout':layout}


def nsis_command(executable, path_option=None, directory=None, flags=()):
    """NSIS treats the final path option as raw text, including any spaces."""
    command = subprocess.list2cmdline([str(executable), '/S', *flags])
    if path_option is not None:
        command += f' {path_option}={directory}'
    return command


def run_nsis(executable, env, path_option=None, directory=None, expected=0, flags=()):
    command = nsis_command(executable, path_option, directory, flags)
    result = subprocess.run(command, executable=str(executable), env=env, timeout=120)
    if result.returncode != expected:
        raise ValueError(f'{executable.name} returned {result.returncode}; expected {expected}')


def windows_process(executable):
    script = "Get-Process -Name Huginn -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $env:HUGINN_SMOKE_EXE } | Select-Object -First 1 -ExpandProperty Id"
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                            env={**os.environ, 'HUGINN_SMOKE_EXE': str(executable)}, capture_output=True, text=True, timeout=30)
    value = result.stdout.strip()
    return int(value) if value.isdigit() else None


def wait_for_windows_process(executable, seconds, running=True):
    deadline = time.monotonic() + seconds
    while True:
        pid = windows_process(executable)
        if (pid is not None) == running or time.monotonic() > deadline:
            return pid
        time.sleep(0.5)


def windows_known_local_app_data():
    import ctypes
    from ctypes import wintypes

    guid = uuid.UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091')
    identifier = (ctypes.c_ubyte * 16).from_buffer_copy(guid.bytes_le)
    shell = ctypes.WinDLL('shell32')
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole = ctypes.WinDLL('ole32')
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    result = ctypes.c_wchar_p()
    status = shell.SHGetKnownFolderPath(ctypes.byref(identifier), 0, None, ctypes.byref(result))
    try:
        if status != 0:
            raise OSError(f'Could not resolve LocalAppData Known Folder (0x{status & 0xffffffff:08x})')
        return Path(result.value)
    finally:
        ole.CoTaskMemFree(result)


LEGACY_UNINSTALL = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Forkrail'


def register_legacy_install(directory):
    import winreg

    for relative in ('Forkrail.exe', 'Forkrail.ico', 'release.json', 'Uninstall.exe', 'service/forkrail-service.exe'):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'legacy ' + relative.encode('ascii'))
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LEGACY_UNINSTALL, 0,
                            winreg.KEY_WRITE | winreg.KEY_WOW64_32KEY) as entry:
        winreg.SetValueEx(entry, 'InstallLocation', 0, winreg.REG_SZ, str(directory))


def legacy_uninstall_entry_exists():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LEGACY_UNINSTALL, 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
            return True
    except FileNotFoundError:
        return False


def windows_uninstall_entry():
    import winreg

    key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Huginn'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as entry:
            return {name: winreg.QueryValueEx(entry, name)[0]
                    for name in ('InstallLocation', 'DisplayVersion', 'UninstallString')}
    except FileNotFoundError:
        return None


def installed_files(directory):
    return {path.relative_to(directory): sha256(path) for path in directory.rglob('*') if path.is_file()}


def require_preserved_markers(*markers):
    for marker in markers:
        if not marker.is_file() or marker.read_text(encoding='utf-8') != 'preserve':
            raise ValueError(f'User data changed during package lifecycle: {marker}')


def smoke_windows_installer(installer, installed, env, folder, marker, version):
    # The installer uses the Windows Known Folder even when the app test sets
    # LOCALAPPDATA explicitly. This path is exercised only on disposable CI.
    default_data = windows_known_local_app_data() / 'Huginn'
    default_data.mkdir(exist_ok=True)
    default_marker = default_data / ('.huginn-release-smoke-' + uuid.uuid4().hex)
    with default_marker.open('x', encoding='utf-8') as handle:
        handle.write('preserve')
    try:
        run_nsis(installer, env, '/D', default_data, expected=5)
        require_preserved_markers(marker, default_marker)

        legacy = folder / 'Programs Forkrail'
        register_legacy_install(legacy)
        legacy_before = installed_files(legacy)
        with windows_file_in_use(legacy / 'Forkrail.exe'):
            run_nsis(installer, env, '/D', installed, expected=3)
        if installed_files(legacy) != legacy_before or not legacy_uninstall_entry_exists() or installed.exists():
            raise ValueError('Locked legacy app was changed by the installer')
        run_nsis(installer, env, '/D', installed)
        if legacy.exists() or legacy_uninstall_entry_exists():
            raise ValueError('Installer left the legacy Forkrail installation or its registration')
        require_preserved_markers(marker, default_marker)
        registration = {
            'InstallLocation': str(installed),
            'DisplayVersion': version,
            'UninstallString': f'"{installed / "Uninstall.exe"}"',
        }
        if windows_uninstall_entry() != registration:
            raise ValueError('Installer did not register the selected installation directory')
        require_preserved_markers(marker, default_marker)

        # The update uses the registered path. A locked app must leave that
        # registration and every installed file intact.
        before = installed_files(installed)
        with windows_file_in_use(installed / 'Huginn.exe'):
            run_nsis(installer, env, expected=3)
            if installed_files(installed) != before or windows_uninstall_entry() != registration:
                raise ValueError('Failed update changed the existing installation')
        require_preserved_markers(marker, default_marker)

        # Model the desktop opening the installer, then finishing its graceful
        # shutdown. A temporary image lock must be retried without forcing exit.
        acquired = threading.Event()
        def closing_app():
            with windows_file_in_use(installed / 'Huginn.exe'):
                acquired.set()
                time.sleep(1.5)
        shutdown = threading.Thread(target=closing_app, daemon=True)
        shutdown.start()
        if not acquired.wait(5):
            raise ValueError('Could not exercise graceful update shutdown')
        try:
            run_nsis(installer, env)
        finally:
            shutdown.join(timeout=5)
        if shutdown.is_alive() or windows_uninstall_entry() != registration:
            raise ValueError('Update did not wait for the closing app')
        require_preserved_markers(marker, default_marker)

        stale = installed / 'service/old-release-only.txt'
        stale.write_text('old', encoding='utf-8')
        run_nsis(installer, env)
        if stale.exists() or windows_uninstall_entry() != registration:
            raise ValueError('Upgrade retained obsolete service files or changed the registered location')
        require_preserved_markers(marker, default_marker)
        result = smoke(installed / 'Huginn.exe', env, folder)

        run_nsis(installer, env, flags=('/RELAUNCH',))
        relaunched = wait_for_windows_process(installed / 'Huginn.exe', 30)
        if relaunched is None:
            raise ValueError('In-app update did not restart Huginn after installing')
        subprocess.run(['taskkill', '/PID', str(relaunched), '/T', '/F'], capture_output=True, timeout=30)
        if wait_for_windows_process(installed / 'Huginn.exe', 30, running=False) is not None:
            raise ValueError('Restarted Huginn did not stop')
        require_preserved_markers(marker, default_marker)

        # The copied uninstaller runs synchronously, so the test observes the
        # actual uninstall exit code and can lock a nested application file.
        uninstaller = folder / 'Uninstall QA.exe'
        shutil.copy2(installed / 'Uninstall.exe', uninstaller)
        before = installed_files(installed)
        locked = installed / 'service/_internal/release.json'
        with windows_file_in_use(locked):
            run_nsis(uninstaller, env, '_?', installed, expected=6)
            if installed_files(installed) != before or windows_uninstall_entry() != registration:
                raise ValueError('Failed uninstall changed the existing installation')
        require_preserved_markers(marker, default_marker)

        run_nsis(uninstaller, env, '_?', installed)
        if installed.exists() or windows_uninstall_entry() is not None:
            raise ValueError('Uninstaller left application files or its registration')
        require_preserved_markers(marker, default_marker)
        return result
    finally:
        default_marker.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--target',required=True)
    args=parser.parse_args();release=config(ROOT)
    if args.target not in release['platforms']:raise ValueError('Unknown target')
    output=ROOT/'dist'/args.target
    name=artifact_stem(release,args.target)
    ci=os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    with tempfile.TemporaryDirectory(prefix='Huginn QA 한글 ') as tmp:
        folder=Path(tmp);home=folder/'home';home.mkdir()
        local=folder/'local';local.mkdir();data=folder/'data';data.mkdir()
        env={**os.environ,'HOME':str(home),'USERPROFILE':str(home),'LOCALAPPDATA':str(local),
             'HUGINN_DATA_DIR':str(data),'PYTHONUTF8':'1'}
        marker=data/'keep-user-data';marker.write_text('preserve',encoding='utf-8')
        result={'target':args.target,'version':release['version'],'installer':None}
        archive = output / (name + '.zip')
        portable = folder / 'portable'
        if args.target.startswith('macos'):
            run(['/usr/bin/ditto', '-x', '-k', archive, portable])
            result['portable'] = smoke_mac_app(portable / 'Huginn.app', release['bundle_id'], env, folder)

            image=output/(name+'.dmg')
            result['app'],result['disk_image']=smoke_mac_disk_image(image,release['bundle_id'],env,folder)
            result['installer']=('DMG mounted, app copied and launched on disposable runner' if ci
                                 else 'DMG copied to temporary Applications; local Applications unchanged')
        else:
            with zipfile.ZipFile(archive) as release_zip:
                release_zip.extractall(portable)
            result['portable'] = smoke(portable / 'Huginn/Huginn.exe', env, folder)
            if ci:
                installed = (local / 'Programs/Huginn').resolve()
                installer = output / (name + '-Setup.exe')
                result['installed'] = smoke_windows_installer(
                    installer, installed, env, folder, marker, release['version'],
                )
                result['installer'] = (
                    'legacy Forkrail install replaced after close, locked upgrade refused, closing app upgrade retried, silent update restarted app, '
                    'upgraded, locked uninstall refused, '
                    'and uninstalled on disposable runner'
                )
            else:result['installer']='not installed on developer machine'
        require_preserved_markers(marker)
        result['user_data_preserved']=True
        (output/'smoke-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
