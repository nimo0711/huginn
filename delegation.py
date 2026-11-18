"""Link external Codex sessions only from explicit Claude launch records."""
import os
from pathlib import Path
import re


THREAD = re.compile(r'^\[codex\] Thread ready \(([0-9a-f-]{36})\)\.', re.M)
_cache = {}


def _thread(path):
    try:
        stamp = (path.stat().st_mtime_ns, path.stat().st_size)
        if path in _cache and _cache[path][0] == stamp:
            return _cache[path][1]
        with path.open(encoding="utf-8") as f:
            head = f.read(4096)
        match = THREAD.search(head) if head.startswith('[codex] Starting Codex task thread.') else None
        child = match.group(1) if match else None
        _cache[path] = (stamp, child)
        return child
    except (OSError, UnicodeError):
        return None


def attach(sessions, output_root=None):
    root = Path(output_root) if output_root else Path('/tmp').resolve() / ('claude-' + str(os.getuid()))
    parents = {s['id']: s for s in sessions if s['agent'] == 'claude'}
    children = {s['id']: s for s in sessions if s['agent'] == 'codex'}
    candidates = {}
    for parent_id in parents:
        for path in root.glob('*/' + parent_id + '/tasks/*.output'):
            child = _thread(path)
            if child in children:
                candidates.setdefault(child, set()).add(parent_id)
    for child_id, options in candidates.items():
        if len(options) != 1:
            continue
        parent_id = next(iter(options))
        child, parent = children[child_id], parents[parent_id]
        child['parent_id'] = parent_id
        # Attribute the delegation to the work active when the child was launched.
        launched = child.get('created_at', 0)
        indices = [i for i, t in enumerate(parent['tasks']) if t['ts'] <= launched]
        task_index = indices[-1] if indices else None
        child['parent_work'] = next((w['key'] for w in parent['works'] if task_index in w['tasks']), None)
