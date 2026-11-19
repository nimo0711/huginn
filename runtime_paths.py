"""Read-only release resources and writable board state."""
import json
import os
from pathlib import Path
import sys
import tempfile


FROZEN = getattr(sys, 'frozen', False)
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


PRODUCT = 'Huginn'
LEGACY_PRODUCT = 'Forkrail'


def user_data_dir(product=PRODUCT):
    home = Path.home()
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local') / product
    if sys.platform == 'darwin':
        return home / 'Library' / 'Application Support' / product
    return Path(os.environ.get('XDG_DATA_HOME') or home / '.local' / 'share') / product


data_override = os.environ.get('HUGINN_DATA_DIR')
if data_override:
    DATA_DIR = Path(data_override).expanduser()
else:
    DATA_DIR = user_data_dir() if FROZEN else RESOURCE_ROOT

DB_PATH = Path(os.environ.get('WORK_BOARD_DB') or DATA_DIR / 'board.db').expanduser()
spool_override = os.environ.get('WORK_BOARD_SPOOL')
if spool_override:
    SPOOL_DIR = Path(spool_override).expanduser()
elif FROZEN or data_override:
    SPOOL_DIR = DATA_DIR / 'spool'
else:
    temp_root = Path(tempfile.gettempdir()) if sys.platform == 'win32' else Path('/tmp')
    SPOOL_DIR = temp_root / 'work-board-spool'


def release_info():
    return json.loads((RESOURCE_ROOT / 'release.json').read_bytes())
