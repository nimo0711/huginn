import sys
import threading
import time

_reported = set()
_lock = threading.Lock()
LIMIT = 1000


def report(where, error):
    key = (where, type(error).__name__, str(error))
    with _lock:
        if key in _reported:
            return
        if len(_reported) >= LIMIT:
            _reported.clear()
        _reported.add(key)
    stream = sys.stderr
    if stream is None:
        return
    print(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {where}: {type(error).__name__}: {error}', file=stream, flush=True)
