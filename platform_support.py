"""Platform-specific primitives used by the board and the bundled service."""
import base64
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


IS_WINDOWS = sys.platform == 'win32'

if IS_WINDOWS:
    import msvcrt
    import psutil
else:
    import fcntl


@contextmanager
def file_lock(path):
    """Lock the same file across action and connection updates."""
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), 'r+b') as lock:
        if IS_WINDOWS:
            # Windows permits locking a byte past EOF, so the existing empty lock
            # files also work and competing writers need not initialize a marker.
            deadline = time.monotonic() + 15
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def replace_file(source, destination):
    """Retry brief Windows reader sharing conflicts with a JSON cache update."""
    deadline = time.monotonic() + 1
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if (not IS_WINDOWS or error.winerror not in (5, 32) or time.monotonic() >= deadline):
                raise
            time.sleep(.02)


def run_hidden(args, **kwargs):
    if IS_WINDOWS:
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


def pid_alive(pid):
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parent_monitor(pid):
    """Return a check tied to the process that started this service."""
    if pid <= 0 or os.getppid() != pid:
        raise ValueError('서비스를 시작한 부모 프로세스를 확인할 수 없습니다.')
    if not IS_WINDOWS:
        return lambda: os.getppid() == pid
    try:
        parent = psutil.Process(pid)
        if parent.create_time() > psutil.Process().create_time() or not parent.is_running():
            raise ValueError('서비스를 시작한 부모 프로세스가 이미 종료되었습니다.')
    except psutil.Error:
        raise ValueError('서비스를 시작한 부모 프로세스를 확인할 수 없습니다.') from None
    return parent.is_running


def cli_path(name, home=None):
    """Resolve installed binaries, skipping cwd search and cmd.exe wrappers."""
    home = Path(home or Path.home())
    if not IS_WINDOWS:
        return next((str(path) for path in (home / '.local/bin' / name,
                    Path('/opt/homebrew/bin') / name, Path('/usr/local/bin') / name)
                    if path.is_file() and os.access(path, os.X_OK)), shutil.which(name))

    path_entries = [home / '.local/bin']
    path_entries.extend(Path(folder.strip('"')) for folder in os.get_exec_path() if folder.strip('"'))
    for folder in path_entries:
        if not folder.is_absolute():
            continue
        candidate = folder / (name + '.exe')
        if candidate.is_file():
            return str(candidate)
    return None


def windows_agent_command(name, home=None):
    """Resolve npm installations via their package entrypoint and native Node."""
    executable = cli_path(name, home)
    if executable:
        return [executable]

    home = Path(home or Path.home())
    entrypoints = {
        'claude': '@anthropic-ai/claude-code/cli.js',
        'codex': '@openai/codex/bin/codex.js',
    }
    if name not in entrypoints:
        return []
    for entry in os.get_exec_path():
        folder = Path(entry.strip('"'))
        if not folder.is_absolute():
            continue
        script = folder / 'node_modules' / entrypoints[name]
        if (folder / (name + '.cmd')).is_file() and script.is_file():
            sibling = folder / 'node.exe'
            node = str(sibling) if sibling.is_file() else cli_path('node', home)
            if node:
                return [node, str(script)]
    return []


_AGENT_ENV = ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')


def write_powershell_launcher(path, args, cwd, *, env=None, label='', remove_agent_context=False):
    """Write a launcher data file. PowerShell never evaluates its contents as code."""
    payload = {
        'exe': args[0],
        'arguments': subprocess.list2cmdline(args[1:]),
        'cwd': str(cwd),
        'env': env or {},
        'removeEnv': list(_AGENT_ENV) if remove_agent_context else [],
        'label': label,
    }
    with path.open('x', encoding='utf-8') as file:
        json.dump(payload, file, ensure_ascii=False)


_POWERSHELL_LAUNCHER = r"""
$ErrorActionPreference = 'Stop'
try {
    $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__FORKRAIL_PATH__'))
    $job = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    Set-Location -LiteralPath $job.cwd
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $job.exe
    $start.Arguments = $job.arguments
    $start.WorkingDirectory = $job.cwd
    $start.UseShellExecute = $false
    foreach ($key in $job.removeEnv) { $start.EnvironmentVariables.Remove($key) }
    foreach ($property in $job.env.PSObject.Properties) {
        $start.EnvironmentVariables[$property.Name] = $property.Value
    }
    if ($job.label) { Write-Host $job.label }
    $process = [Diagnostics.Process]::Start($start)
    try {
        $process.WaitForExit()
        $code = $process.ExitCode
    } finally { $process.Dispose() }
    $global:LASTEXITCODE = $code
} catch {
    Write-Error -ErrorAction Continue 'Huginn could not open the selected command.'
    $global:LASTEXITCODE = 1
}
__FORKRAIL_EXIT__
"""


def powershell_command(path, *, stay_open=True):
    windows = Path(os.environ.get('SystemRoot') or r'C:\Windows')
    powershell = windows / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = _POWERSHELL_LAUNCHER.replace('__FORKRAIL_PATH__', base64.b64encode(str(path).encode('utf-8')).decode('ascii'))
    script = script.replace('__FORKRAIL_EXIT__', '' if stay_open else 'exit $global:LASTEXITCODE')
    options = ['-NoLogo', '-NoProfile', *(['-NoExit'] if stay_open else ['-NonInteractive'])]
    return [str(powershell), *options, '-EncodedCommand', base64.b64encode(script.encode('utf-16-le')).decode('ascii')]
