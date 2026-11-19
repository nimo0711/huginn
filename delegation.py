"""Read delegation evidence locally and link only explicit provider IDs."""
import os
import copy
import json
from datetime import datetime
from pathlib import Path
import re

from platform_support import IS_WINDOWS


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


def _work(parent, launched):
    indices = [i for i, t in enumerate(parent.get('tasks', [])) if t['ts'] <= launched]
    index = indices[-1] if indices else None
    return next((w['key'] for w in parent.get('works', []) if index in w['tasks']), None)


def codex_parent(source):
    try:
        source = json.loads(source) if isinstance(source, str) else source
        value = source['subagent']['thread_spawn']['parent_thread_id']
        return value if isinstance(value, str) else ''
    except (ValueError, KeyError, TypeError):
        return ''


def attach(sessions, output_root=None):
    """Only provider IDs or explicit launch output can establish ownership."""
    by_id = {s['id']: s for s in sessions}
    candidates = {}
    for child in sessions:
        if child.get('source_parent') in by_id:
            candidates.setdefault(child['id'], set()).add(child['source_parent'])
    root = Path(output_root) if output_root else (None if IS_WINDOWS else Path('/tmp').resolve() / ('claude-' + str(os.getuid())))
    if root:
        for parent in sessions:
            if parent['agent'] != 'claude':
                continue
            for path in root.glob('*/' + parent['id'] + '/tasks/*.output'):
                child = _thread(path)
                if child in by_id and by_id[child]['agent'] == 'codex':
                    candidates.setdefault(child, set()).add(parent['id'])
    links = {child: next(iter(options)) for child, options in candidates.items() if len(options) == 1}
    for child_id, parent_id in links.items():
        seen, cursor = set(), parent_id
        while cursor in links and cursor != child_id and cursor not in seen:
            seen.add(cursor)
            cursor = links[cursor]
        if cursor == child_id:
            continue
        child, parent = by_id[child_id], by_id[parent_id]
        child['parent_id'] = parent_id
        child['parent_work'] = _work(parent, child.get('created_at', 0))
        first = next(iter(child.get('tasks', [])), {})
        child['handoff'] = {'prompt': first.get('instruction', ''),
                            'ts': child.get('created_at') or first.get('ts', 0)}


MAX_TEXT = 12000
MAX_ITEMS = 40
_child_cache = {}


def text(value):
    if isinstance(value, list):
        value = '\n'.join(b['text'] for b in value if isinstance(b, dict) and isinstance(b.get('text'), str)
                          and b.get('type') in ('text', 'input_text', 'output_text'))
    if not isinstance(value, str):
        return ''
    value = value.strip()
    return value[:MAX_TEXT] + ('\n… (이후 내용 생략)' if len(value) > MAX_TEXT else '')


def record_call(task, block, ts):
    if block.get('name') not in ('Agent', 'Task'):
        return
    inp = block.get('input') if isinstance(block.get('input'), dict) else {}
    rows = task.setdefault('delegations', [])
    ident = block.get('id')
    if not isinstance(ident, str) or not ident or any(r['id'] == ident for r in rows):
        return
    rows.append({'id': ident, 'agent': 'claude', 'label': text(inp.get('description') or inp.get('subagent_type'))[:160],
                 'prompt': text(inp.get('prompt')), 'result': '', 'activity': '', 'status': 'running',
                 'ts': ts, 'last_ts': ts, 'end_ts': 0, 'parent_task_ts': task['ts'],
                 'background': bool(inp.get('run_in_background'))})
    del rows[:-MAX_ITEMS]


def record_result(tasks, tool_id, content, ts, error=False, metadata=None):
    row = next((r for t in reversed(tasks) for r in t.get('delegations', []) if r['id'] == tool_id), None)
    if row is None:
        return
    metadata = metadata if isinstance(metadata, dict) else {}
    child_id = metadata.get('agentId')
    if isinstance(child_id, str) and child_id:
        row['child_id'] = child_id
    row['last_ts'] = ts
    if not error and (row.get('background') or metadata.get('isAsync')):
        return  # An async launch receipt is not the delegated result.
    row.update(result=text(content), end_ts=ts, status='failed' if error else 'completed')


def codex_call(task, body, ts):
    # The collab event stores receiver IDs and the exact supplied prompt.
    if not body.get('prompt') or body.get('tool') not in ('spawnAgent', 'spawn_agent', 'sendInput', 'send_input'):
        return
    receivers = body.get('receiverThreadIds')
    ident = body.get('id')
    if not isinstance(receivers, list) or not isinstance(ident, str):
        return
    for child_id in receivers:
        if not isinstance(child_id, str):
            continue
        rows = task.setdefault('delegations', [])
        rows.append({'id': ident + ':' + child_id, 'child_id': child_id, 'agent': 'codex',
                     'label': '위임 작업', 'prompt': text(body['prompt']), 'result': '', 'activity': '',
                     'status': 'running', 'ts': ts, 'last_ts': ts, 'end_ts': 0, 'parent_task_ts': task['ts']})
        del rows[:-MAX_ITEMS]


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def read_child(path):
    """Incremental local JSONL read, bounded retained text; no model invocation."""
    path = Path(path)
    try:
        stat = path.stat()
        stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        saved = _child_cache.get(path)
        if saved and saved[0] == stamp:
            return copy.deepcopy(saved[2])
        if saved and saved[0][0] == stat.st_ino and stat.st_size > saved[0][1]:
            offset, row = saved[1], copy.deepcopy(saved[2])
        else:
            offset, row = 0, {'prompt': '', 'result': '', 'activity': '', 'ts': 0, 'last_ts': 0,
                              'end_ts': 0, 'status': 'running', 'wait': None, 'last_message': '', 'pending': {}}
        with path.open('rb') as stream:
            stream.seek(offset)
            for line in stream:
                if not line.endswith(b'\n'):
                    break
                offset = stream.tell()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                ts = _timestamp(event.get('timestamp'))
                row['ts'] = row['ts'] or ts
                row['last_ts'] = max(row['last_ts'], ts)
                message = event.get('message')
                if not isinstance(message, dict):
                    continue
                content = message.get('content')
                kind = event.get('type')
                if kind == 'user':
                    instruction = text(content)
                    if instruction.startswith('[Request interrupted'):
                        row.update(status='interrupted', end_ts=ts, result='', wait=None, pending={})
                        continue
                    if not row['prompt'] and instruction and not event.get('isMeta'):
                        row['prompt'] = instruction
                    if instruction and not event.get('isMeta'):
                        row.update(status='running', end_ts=0, result='', last_message='')
                    for b in content if isinstance(content, list) else []:
                        if isinstance(b, dict) and b.get('type') == 'tool_result':
                            if isinstance(b.get('tool_use_id'), str):
                                row['pending'].pop(b['tool_use_id'], None)
                            if row['wait'] and row['wait'].get('id') == b.get('tool_use_id'):
                                row['wait'] = None
                if kind != 'assistant':
                    continue
                row['status'], row['end_ts'], row['result'] = 'running', 0, ''
                note = text(content)
                if note:
                    row['last_message'] = note
                    row['activity'] = note[:600]
                for b in content if isinstance(content, list) else []:
                    if not isinstance(b, dict) or b.get('type') != 'tool_use':
                        continue
                    if not isinstance(b.get('id'), str) or not isinstance(b.get('name'), str):
                        continue
                    name = b['name']
                    inp = b.get('input') if isinstance(b.get('input'), dict) else {}
                    row['pending'][b['id']] = name
                    if len(row['pending']) > 256:
                        row['pending'].pop(next(iter(row['pending'])))
                        row['pending_overflow'] = True
                    row['activity'] = text(inp.get('description') or name)[:600]
                    if name == 'AskUserQuestion':
                        questions = inp.get('questions')
                        row['wait'] = {'id': b['id'], 'text': text(' / '.join(text(q.get('question')) for q in questions if isinstance(q, dict))) if isinstance(questions, list) else '', 'ts': ts}
                if message.get('stop_reason') == 'end_turn' and not row['pending'] and not row['wait'] and not row.get('pending_overflow'):
                    row.update(status='completed', result=row['last_message'], end_ts=ts)
        _child_cache[path] = (stamp, offset, row)
        if len(_child_cache) > 256:
            del _child_cache[next(iter(_child_cache))]
        return copy.deepcopy(row)
    except OSError:
        return {'prompt': '', 'result': '', 'activity': '', 'status': 'unknown', 'ts': 0, 'last_ts': 0, 'end_ts': 0, 'wait': None}


def claude_children(base, tasks, now):
    calls = {r['id']: r for t in tasks for r in t.get('delegations', [])}
    items = []
    paths = list((Path(base) / 'subagents').glob('*.meta.json'))
    paths.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for meta_path in paths[:MAX_ITEMS]:
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            if not isinstance(meta, dict):
                continue
        except (OSError, ValueError):
            continue
        tool_id = meta.get('toolUseId')
        tool_id = tool_id if isinstance(tool_id, str) else ''
        call = calls.get(tool_id, {})
        row = read_child(meta_path.with_name(meta_path.name.replace('.meta.json', '.jsonl')))
        if call.get('status') in ('failed', 'completed'):
            row.update(status=call['status'], end_ts=call.get('end_ts', 0),
                       result=call.get('result') or row.get('result', ''), wait=None)
        elif meta.get('finished'):
            row.update(status='completed', end_ts=row.get('end_ts') or row.get('last_ts', 0),
                       result=row.get('result') or row.get('last_message', ''), wait=None)
        elif row['status'] not in ('completed', 'failed', 'interrupted') and now - row.get('last_ts', 0) > 900:
            row['status'] = 'unknown'
        if row.get('wait'):
            row['status'] = 'waiting'
        started = call.get('ts') or row.get('ts', 0)
        parent_ts = call.get('parent_task_ts')
        if parent_ts is None:
            parent_ts = next((t['ts'] for t in reversed(tasks) if t['ts'] <= started), None)
        row.update(id=tool_id or meta_path.stem, agent='claude',
                   label=text(meta.get('description') or meta.get('agentType'))[:160],
                   prompt=call.get('prompt') or row['prompt'], ts=started, parent_task_ts=parent_ts)
        row.pop('pending', None)
        row.pop('last_message', None)
        items.append(row)
    return {'items': items, 'done': sum(r['status'] == 'completed' for r in items),
            'running': [{'what': r['label'], 'ts': r['ts'], 'last': r['last_ts'], 'parent_task_ts': r['parent_task_ts']}
                        for r in items if r['status'] in ('running', 'waiting')]}
