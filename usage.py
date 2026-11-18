from __future__ import annotations

import glob
import json
import os
import threading
import time
import connections
from datetime import datetime, timedelta

import blocks

PROJECTS = os.path.expanduser("~/.claude/projects")
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
CODEX_SESSIONS = os.path.expanduser('~/.codex/sessions')
CODEX_ARCHIVE = os.path.expanduser('~/.codex/archived_sessions')
DAYS = 7
TTL = 60

_files: dict = {}
_codex_files: dict = {}
_result: dict = {}
_lock = threading.Lock()
_busy = threading.Event()


def _day(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
    except (ValueError, TypeError, AttributeError):
        return ""


def _session_of(path: str) -> str:
    parts = path.split(os.sep)
    if "subagents" in parts:
        return parts[parts.index("subagents") - 1]
    return os.path.splitext(parts[-1])[0]


SKIP_PROMPT = ("<command-", "<local-command", "Caveat:", "<task-notification", "This session is being continued", "<system-reminder", "[Request interrupted")


def _bump(d: dict, *keys, n: int = 1) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = d.get(keys[-1], 0) + n


def _is_prompt(e: dict) -> bool:
    if e.get("isMeta") or e.get("isSidechain"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    c = (c or "").strip() if isinstance(c, str) else ""
    return bool(c) and not c.startswith(SKIP_PROMPT)


def _scan_file(path: str, size: int) -> None:
    st = _files.get(path)
    if st is None or size < st["offset"]:
        st = {"offset": 0, "ids": {}, "days": {}, "tool_ids": {}, "tool_days": {}, "error_ids":set(), "s": {}, "partial": set()}
        _files[path] = st
    if size <= st["offset"]:
        return
    sub = "subagents" in path.split(os.sep)
    S = st["s"]
    with open(path, "rb") as f:
        f.seek(st["offset"])
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            st["offset"] = f.tell()
            try:
                if not (b'"assistant"' in raw or b'"user"' in raw):
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    st['partial'].add('malformed')
                    continue
                if not isinstance(e, dict):
                    st['partial'].add('malformed')
                    continue
                day = _day(e.get("timestamp") or "")
                if not day:
                    continue
                if e.get("cwd") and not st.get("cwd"):
                    st["cwd"] = e["cwd"]
                kind = e.get("type")
                if kind == "system":
                    continue
                msg = e.get("message") or {}
                if not isinstance(msg, dict):
                    st['partial'].add('malformed')
                    continue
                if kind == "user":
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get('type') == 'tool_result':
                                tid = b.get('tool_use_id')
                                if tid in st['tool_ids'] and tid not in st.setdefault('result_ids', set()):
                                    call_day = st['tool_days'][tid]
                                    field = 'blocked' if b.get('is_error') and blocks.is_blocked(b.get('content')) else 'checked'
                                    _bump(S, field, call_day, st['tool_ids'][tid])
                                    st['result_ids'].add(tid)
                                    if b.get('is_error') and field != 'blocked':
                                        _bump(S, 'errors', call_day, st['tool_ids'][tid])
                    if not sub and _is_prompt(e):
                        _bump(S, "prompts", day)
                    continue
                if kind != "assistant":
                    continue
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        if b.get('id') and b['id'] in st['tool_ids']:continue
                        name = b.get("name") or "기타"
                        name = "MCP" if name.startswith("mcp__") else name
                        st["tool_ids"][b.get("id") or ""] = name
                        st['tool_days'][b.get('id') or ''] = day
                        _bump(S, "tools", day, name)
                u = msg.get("usage")
                mid = msg.get("id")
                if not isinstance(u, dict) or not mid:
                    continue
                previous=st['ids'].get(mid)
                try:
                    values={k:max(0,int(u.get(k,(previous or {}).get('values',{}).get(k,0)) or 0)) for k in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens','output_tokens')}
                    hour=datetime.fromisoformat(e['timestamp'].replace('Z','+00:00')).astimezone().hour
                except (ValueError,TypeError,KeyError,OverflowError):continue
                model=str(msg.get('model') or (previous or {}).get('model') or '')
                if previous:
                    if values==previous['values'] and model==previous['model']:continue
                    old_total=sum(previous['values'].values());old_day=previous['day']
                    st['days'][old_day]-=old_total
                    _bump(S,'out',old_day,n=-previous['values']['output_tokens'])
                    _bump(S,'hours',old_day,previous['hour'],n=-old_total)
                    if previous['model'] and not previous['model'].startswith('<'):_bump(S,'models',old_day,previous['model'],n=-old_total)
                    day=old_day;hour=previous['hour']
                st['ids'][mid]={'values':values,'day':day,'hour':hour,'model':model}
                total = sum(values.values())
                st["days"][day] = st["days"].get(day, 0) + total
                if model and not model.startswith("<"):
                    _bump(S, "models", day, model, n=total)
                _bump(S, "out", day, n=values['output_tokens'])
                _bump(S, "hours", day, hour, n=total)
            except (ValueError, TypeError, AttributeError, OverflowError):
                # A malformed complete record must not become an invisible gap.
                st['partial'].add('malformed')


def _project(st: dict) -> str:
    cwd = st.get("cwd") or ""
    home = os.path.expanduser("~")
    return ("~" + cwd[len(home):]) if cwd.startswith(home) else (cwd or "알 수 없음")


def _limit() -> dict:
    try:
        with open(CLAUDE_JSON, encoding="utf-8") as f:
            cached = json.load(f).get("cachedUsageUtilization") or {}
    except (OSError, ValueError):
        return {}
    week = (cached.get("utilization") or {}).get("seven_day") or {}
    if week.get("utilization") is None:
        return {}
    resets = week.get("resets_at") or ""
    try:
        reset_ts = datetime.fromisoformat(resets).timestamp()
    except ValueError:
        reset_ts = 0
    return {
        "percent": week["utilization"],
        "at": (cached.get("fetchedAtMs") or 0) / 1000,
        "resets": reset_ts,
        "stale": bool(reset_ts and reset_ts < time.time()),
    }


def _when(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
    except (ValueError, TypeError, AttributeError):
        return None


def _token_values(value):
    if not isinstance(value, dict) or not {'input_tokens', 'output_tokens'} <= value.keys():
        return None
    try:
        values = tuple(int(value[key]) for key in ('input_tokens', 'output_tokens'))
        return values if min(values) >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _outcome(value):
    """Only protocol fields establish failure; arbitrary output prose never does."""
    if not isinstance(value, dict):
        return 'unknown'
    status = str(value.get('status', '')).lower()
    if status in ('declined', 'denied', 'rejected', 'blocked', 'cancelled', 'canceled'):
        return 'blocked'
    result = value.get('result')
    if isinstance(result, dict) and result.get('isError') is True:
        return 'error'
    if isinstance(value.get('exit_code'), int):
        return 'error' if value['exit_code'] else 'ok'
    for field in ('isError', 'is_error'):
        if isinstance(value.get(field), bool):
            return 'error' if value[field] else 'ok'
    if isinstance(value.get('success'), bool):
        return 'ok' if value['success'] else 'error'
    return 'error' if status == 'failed' else 'ok' if status == 'completed' else 'unknown'


def _output_outcome(output):
    if isinstance(output, dict):
        return _outcome(output)
    if isinstance(output, str):
        if blocks.is_blocked(output):
            return 'blocked'
        try:
            return _outcome(json.loads(output))
        except (ValueError, TypeError):
            return 'unknown'
    # A tool wrapper may print many nested results. They are not its own outcome.
    return 'unknown'


CODEX_TOOLS = {'commandexecution': '명령 실행', 'filechange': '파일 변경', 'mcptoolcall': 'MCP',
               'websearch': '웹 검색', 'imageview': '이미지 보기', 'imagegeneration': '이미지 생성',
               'extension': '확장 도구', 'dynamictoolcall': '동적 도구', 'collabagenttoolcall': '에이전트 협업'}


def _scan_codex_file(path, stat):
    st = _codex_files.get(path)
    if st is None or stat.st_ino != st['inode'] or stat.st_size < st['offset']:
        st = {'offset': 0, 'inode': stat.st_ino, 'sid': '', 'cwd': '', 'model': 'Codex 모델 미확인',
              'created': None, 'sub': False, 'fork': False, 'previous': None, 'last_ts': None,
              'snapshots': set(), 'tokens': {}, 'prompts': {}, 'native': {}, 'calls': {},
              'partial': set(), 'record': None, 'token_events': 0, 'token_days': set()}
        _codex_files[path] = st
    if stat.st_size <= st['offset']:
        return st
    with open(path, 'rb') as stream:
        stream.seek(st['offset'])
        for raw in stream:
            if not raw.endswith(b'\n'):
                break
            st['offset'] = stream.tell()
            try:
                event = json.loads(raw)
            except ValueError:
                st['partial'].add('malformed')
                continue
            if not isinstance(event, dict):
                continue
            kind, payload = event.get('type'), event.get('payload')
            if not isinstance(payload, dict):
                continue
            when = _when(event.get('timestamp'))
            if kind == 'session_meta' and not st['sid']:
                source = payload.get('source')
                st.update(sid=payload.get('id') or payload.get('session_id') or path,
                          cwd=payload.get('cwd') or '', created=_when(payload.get('timestamp')),
                          sub=payload.get('thread_source') == 'subagent' or isinstance(source, dict) and 'subagent' in source,
                          fork=bool(payload.get('forked_from_id')))
                continue
            if kind == 'turn_context':
                st['model'] = payload.get('model') or st['model']
                st['cwd'] = payload.get('cwd') or st['cwd']
                continue
            if kind == 'token_usage_record':
                st['record'] = (payload.get('response_id'), payload.get('thread_id'), _token_values(payload.get('thread_token_usage')))
                continue
            if when is None:
                continue
            day, hour = when.strftime('%Y-%m-%d'), when.hour
            inherited = bool(st['created'] and when < st['created'])
            owner = payload.get('thread_id')
            inherited |= bool(owner and st['sid'] and owner != st['sid'])
            if kind == 'event_msg' and payload.get('type') == 'token_count':
                info = payload.get('info') or {}
                current = _token_values(info.get('total_token_usage')) if isinstance(info, dict) else None
                if current is None:
                    if info:
                        st['partial'].add('missing_total')
                    continue
                st['token_events'] += 1
                key = (event['timestamp'], *current)
                if key in st['snapshots'] or st['last_ts'] and when < st['last_ts']:
                    continue
                st['snapshots'].add(key)
                last = _token_values(info.get('last_token_usage'))
                previous = st['previous']
                if previous is None or any(a < b for a, b in zip(current, previous)):
                    if last is not None and all(a <= b for a, b in zip(last, current)):
                        delta = last
                        if previous is None and last != current:
                            st['partial'].add('initial_baseline')
                    else:
                        delta = (0, 0)
                        st['partial'].add('counter_reset' if previous else 'initial_baseline')
                else:
                    delta = tuple(a - b for a, b in zip(current, previous))
                st['previous'], st['last_ts'] = current, when
                record = st['record']
                if record and record[2] == current and record[1] and record[1] != st['sid']:
                    inherited = True
                if not inherited:
                    st['token_days'].add(day)
                if inherited or not sum(delta):
                    continue
                identity = ('response', record[0]) if record and record[0] and record[2] == current else (st['sid'] or path, *key)
                old = st['tokens'].get(identity)
                st['tokens'][identity] = (old[0], old[1], old[2], old[3] + sum(delta), old[4] + delta[1]) if old else (day, hour, st['model'], sum(delta), delta[1])
                continue
            if inherited:
                continue
            if kind == 'event_msg' and payload.get('type') in ('item_completed', 'user_message'):
                item = payload.get('item') if payload.get('type') == 'item_completed' else payload
                if not isinstance(item, dict):
                    continue
                item_kind = str(item.get('type', '')).replace('_', '').lower()
                if item_kind in ('usermessage',) and not st['sub']:
                    identifier = item.get('client_id') or item.get('id') or (payload.get('turn_id'), event['timestamp'])
                    content = item.get('content') or []
                    text = item.get('message')
                    if text is None and isinstance(content, list):
                        text = ' '.join(b['text'] for b in content if isinstance(b, dict) and isinstance(b.get('text'), str))
                    if not isinstance(text, str):
                        st['partial'].add('malformed_prompt')
                        continue
                    if text.strip() and not text.strip().startswith(SKIP_PROMPT + ('<cross-session-message',)):
                        st['prompts'][identifier] = day
                elif item_kind in CODEX_TOOLS and item.get('id'):
                    st['native'][item['id']] = (day, CODEX_TOOLS[item_kind], _outcome(item), when)
                continue
            if kind == 'response_item':
                call_id = payload.get('call_id')
                if not call_id:
                    continue
                if payload.get('type') in ('function_call', 'custom_tool_call'):
                    name = payload.get('name') or '기타'
                    name = 'MCP' if name.startswith('mcp__') else name
                    st['calls'].setdefault(call_id, (day, name, 'unknown', when))
                elif payload.get('type') in ('function_call_output', 'custom_tool_call_output') and call_id in st['calls']:
                    call_day, name, _, _ = st['calls'][call_id]
                    st['calls'][call_id] = (call_day, name, _output_outcome(payload.get('output')), when)
    return st


def _codex_states(paths):
    """Normalize only counters and identifiers, retaining no prompt or tool output."""
    token_rows, seen_prompts, tool_rows = {}, set(), {}
    rows, partial = [], set()
    token_events = 0
    cutoff = (datetime.now() - timedelta(days=DAYS - 1)).strftime('%Y-%m-%d')
    for path in sorted(paths):
        raw = _scan_codex_file(path, os.stat(path))
        sid = raw['sid'] or path
        state = {'sid': sid, 'cwd': raw['cwd'], 'days': {}, 's': {}}
        S = state['s']; partial.update(raw['partial'])
        observed = any(day >= cutoff for day in raw['token_days'])
        token_events += int(observed)
        activity = any(day >= cutoff for day in raw['prompts'].values()) or any(value[0] >= cutoff for source in ('native', 'calls') for value in raw[source].values())
        if activity and not observed:
            partial.add('activity_without_tokens')
        for identifier, value in raw['tokens'].items():
            # Archived/copied rollouts may contain an older version of one response.
            if identifier not in token_rows or value[3] > token_rows[identifier][1][3]:
                token_rows[identifier] = (state, value)
        for identifier, day in raw['prompts'].items():
            if (sid, identifier) not in seen_prompts:
                seen_prompts.add((sid, identifier)); _bump(S, 'prompts', day)
        for identifier, value in (raw['native'] or raw['calls']).items():
            key = (sid, identifier)
            previous = tool_rows.get(key)
            # Prefer confirmed outcomes, then the latest result timestamp, across copies.
            if previous is None or (value[2] != 'unknown', value[3]) > (previous[1][2] != 'unknown', previous[1][3]):
                tool_rows[key] = (state, value)
        rows.append(state)
    for state, (day, hour, model, total, output) in token_rows.values():
        _bump(state['days'], day, n=total)
        _bump(state['s'], 'out', day, n=output)
        _bump(state['s'], 'hours', day, hour, n=total)
        _bump(state['s'], 'models', day, model, n=total)
    for state, (day, name, outcome, _) in tool_rows.values():
        S = state['s']; _bump(S, 'tools', day, name)
        if outcome in ('ok', 'error'):
            _bump(S, 'checked', day, name)
        if outcome == 'error':
            _bump(S, 'errors', day, name)
        if outcome == 'blocked':
            _bump(S, 'blocked', day, name)
    return rows, partial, token_events


def _aggregate(states, provider):
    today = datetime.now().strftime('%Y-%m-%d')
    week = sorted((datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(DAYS))
    fields = ('tokens', 'out', 'prompts', 'tools', 'errors', 'checked', 'blocked')
    daily = {d: dict(day=d, sessions=set(), **{key: 0 for key in fields}) for d in week}
    models, tools, projects, session_tokens = {}, {}, {}, {}
    hours, sessions_week = [0] * 24, set()
    for state in states:
        S, sid, project = state['s'], state['sid'], _project(state)
        for day in week:
            D = daily[day]; tokens = state['days'].get(day, 0)
            prompts = S.get('prompts', {}).get(day, 0)
            calls = S.get('tools', {}).get(day, {})
            D['tokens'] += tokens; D['out'] += S.get('out', {}).get(day, 0); D['prompts'] += prompts
            if tokens or prompts or calls:
                D['sessions'].add(sid); sessions_week.add(sid)
                P = projects.setdefault(project, {'project': project, 'tokens': 0, 'sessions': set(), 'prompts': 0, 'tools': 0})
                P['tokens'] += tokens; P['sessions'].add(sid); P['prompts'] += prompts; P['tools'] += sum(calls.values())
            if day == today and tokens:
                session_tokens[sid] = session_tokens.get(sid, 0) + tokens
            for source, field in (('tools', 'count'), ('errors', 'errors'), ('checked', 'checked'), ('blocked', 'blocked')):
                values = S.get(source, {}).get(day, {})
                D[source] += sum(values.values())
                for name, value in values.items():
                    _bump(tools, name, field, n=value)
            for model, value in S.get('models', {}).get(day, {}).items():
                models[model] = models.get(model, 0) + value
        for hour, value in S.get('hours', {}).get(today, {}).items():
            hours[int(hour)] += value
    tool_rows = []
    for name, value in tools.items():
        row = dict(tool=name, **{key: value.get(key, 0) for key in ('count', 'errors', 'checked', 'blocked')})
        row['unknown'] = max(0, row['count'] - row['checked'] - row['blocked']); tool_rows.append(row)
    day_rows = []
    for value in daily.values():
        day_rows.append(dict(value, sessions=len(value['sessions']), unknown=max(0, value['tools'] - value['checked'] - value['blocked'])))
    return {'provider': provider, 'at': time.time(), 'today': daily[today]['tokens'], 'week': sum(d['tokens'] for d in daily.values()),
            'days': [{'day': day, 'tokens': daily[day]['tokens']} for day in week],
            'sessions': [{'id': key, 'tokens': value} for key, value in sorted(session_tokens.items(), key=lambda x: -x[1])[:8]],
            'limit': _limit() if provider == 'claude' else {},
            'stats': {'daily': day_rows, 'models': sorted(({'model': key, 'tokens': value} for key, value in models.items()), key=lambda x: -x['tokens']),
                      'tools': sorted(tool_rows, key=lambda x: -x['count']),
                      'projects': sorted((dict(p, sessions=len(p['sessions'])) for p in projects.values()), key=lambda x: -x['tokens']),
                      'hours': hours, 'week_sessions': len(sessions_week)}}


def _collect(provider, previous):
    label = 'Claude' if provider == 'claude' else 'Codex'
    if not connections.STORE.get(provider)['enabled']:
        return {'provider': provider, 'disabled': True}
    try:
        roots = [PROJECTS] if provider == 'claude' else [CODEX_SESSIONS, CODEX_ARCHIVE]
        paths = [path for root in roots if os.path.isdir(root) for path in glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True)]
        if not paths:
            return {'provider': provider, 'unavailable': True, 'error': label + '의 집계할 로컬 기록이 없습니다.', 'attempt_at': time.time()}
        cutoff = time.time() - (DAYS + 1) * 86400
        paths = [path for path in paths if os.stat(path).st_mtime >= cutoff]
        if provider == 'claude':
            states = []
            for path in paths:
                _scan_file(path, os.stat(path).st_size)
                states.append(dict(_files[path], sid=_session_of(path)))
            result = _aggregate(states, provider)
            notes = ['로컬 Claude 기록 기준이며 하위 에이전트 사용량을 포함합니다.', '입력·캐시 생성·캐시 읽기·출력 토큰을 합산합니다.']
            partial = set().union(*(s.get('partial', set()) for s in states))
        else:
            states, partial, token_events = _codex_states(paths)
            result = _aggregate(states, provider)
            notes = ['로컬 Codex 기록 기준이며 하위 에이전트 토큰을 포함합니다.', '누적 입력·출력의 증가분만 합산하며 캐시·추론 토큰을 다시 더하지 않습니다.',
                     '기록된 실행 결과를 집계하며 일부 도구 래퍼는 포함되지 않습니다.']
            if partial:
                notes.append('초기 누적값·불완전한 기록 중 확인할 수 없는 사용량은 합산하지 않았습니다.')
            if 'activity_without_tokens' in partial:
                notes.append('요청·도구 활동은 있으나 토큰 기록이 없는 세션은 토큰 합계에서 빠져 있습니다.')
            if paths and not token_events:
                partial.add('no_tokens'); notes.append('Codex 토큰 기록은 아직 확인되지 않았습니다.')
        unknown = sum(t['unknown'] for t in result['stats']['tools'])
        notes.append('실패율은 결과가 확인된 실행 기준이며 승인 차단은 제외합니다.')
        if unknown:
            notes.append('결과 미확인 ' + str(unknown) + '건은 실패율 분모에서 제외합니다.')
        result['tokens_unavailable'] = 'no_tokens' in partial
        result['coverage'] = {'tokens': 'unavailable' if result['tokens_unavailable'] else 'partial' if partial else 'complete', 'prompts': 'partial' if partial else 'complete',
                              'tools': 'partial' if provider == 'codex' else 'complete', 'errors': 'partial' if unknown else 'complete', 'notes': notes}
        result['partial'] = bool(partial)
        return result
    except (OSError, ValueError, TypeError, OverflowError):
        return {**previous, 'provider': provider, 'error': label + ' 기록을 읽지 못했습니다. 접근 권한을 확인해 주세요.',
                'stale': bool(previous.get('at')), 'unavailable': not bool(previous.get('at')), 'attempt_at': time.time()}


def _merge_rows(sources, name, identity, fields):
    rows = {}
    for source in sources:
        for row in source.get('stats', {}).get(name, []):
            combined = rows.setdefault(row[identity], {identity: row[identity], **{field: 0 for field in fields}})
            for field in fields:
                combined[field] += row.get(field, 0)
    return list(rows.values())


def _combine(providers):
    enabled = [p for p in providers.values() if not p.get('disabled')]
    usable = [p for p in enabled if p.get('at') and p.get('stats')]
    out = {'provider': 'all', 'providers': providers, 'limit': {}}
    if not enabled:
        return dict(out, disabled=True)
    if not usable:
        return dict(out, unavailable=True, error='집계할 로컬 사용 기록을 확인하지 못했습니다.', attempt_at=max((p.get('attempt_at', 0) for p in enabled), default=0))
    out.update(at=min(p['at'] for p in usable), today=sum(p['today'] for p in usable), week=sum(p['week'] for p in usable))
    daily = _merge_rows(usable, 'daily', 'day', ('tokens', 'out', 'prompts', 'tools', 'errors', 'sessions', 'checked', 'unknown', 'blocked'))
    out['days'] = [{'day': d['day'], 'tokens': d['tokens']} for d in daily]
    out['sessions'] = sorted((dict(row, provider=p['provider']) for p in usable for row in p.get('sessions', [])), key=lambda row: -row['tokens'])[:8]
    out['stats'] = {'daily': daily, 'models': sorted(_merge_rows(usable, 'models', 'model', ('tokens',)), key=lambda x: -x['tokens']),
                    'tools': sorted(_merge_rows(usable, 'tools', 'tool', ('count', 'errors', 'checked', 'unknown', 'blocked')), key=lambda x: -x['count']),
                    'projects': sorted(_merge_rows(usable, 'projects', 'project', ('tokens', 'sessions', 'prompts', 'tools')), key=lambda x: -x['tokens']),
                    'hours': [sum(p['stats']['hours'][h] for p in usable) for h in range(24)],
                    'week_sessions': sum(p['stats']['week_sessions'] for p in usable)}
    out['partial'] = len(usable) != len(enabled) or any(p.get('partial') or p.get('stale') for p in usable)
    out['tokens_unavailable'] = all(p.get('tokens_unavailable') or not p.get('at') for p in enabled)
    out['stale'] = any(p.get('stale') for p in usable)
    out['attempt_at'] = max((p.get('attempt_at', p.get('at', 0)) for p in enabled), default=0)
    notes = list(dict.fromkeys(note for p in enabled for note in p.get('coverage', {}).get('notes', [])))
    if len(usable) != len(enabled):
        notes.append('확인할 수 있는 제공자의 기록만 전체 합계에 포함했습니다.')
    out['coverage'] = {key: 'partial' if any(p.get('coverage', {}).get(key) != 'complete' for p in enabled) else 'complete'
                       for key in ('tokens', 'prompts', 'tools', 'errors')}
    out['coverage']['notes'] = notes
    if out['tokens_unavailable']:
        out['coverage']['tokens'] = 'unavailable'
    if out['stale']:
        out['error'] = '일부 제공자의 이전 집계가 표시됩니다.'
    return out


def _refresh():
    try:
        with _lock:
            previous = dict(_result)
        providers = {name: _collect(name, previous.get('providers', {}).get(name, previous if name == 'claude' and 'providers' not in previous else {}))
                     for name in ('claude', 'codex')}
        for name in providers:
            if not connections.STORE.get(name)['enabled']:
                providers[name] = {'provider': name, 'disabled': True}
        with _lock:
            _result.clear(); _result.update(_combine(providers))
    finally:
        _busy.clear()


def summary():
    with _lock:
        out = dict(_result)
    enabled = {name: connections.STORE.get(name)['enabled'] for name in ('claude', 'codex')}
    changed = False
    if out.get('providers'):
        providers = dict(out['providers'])
        for name, active in enabled.items():
            if not active:
                changed |= not providers.get(name, {}).get('disabled', False)
                providers[name] = {'provider': name, 'disabled': True}
            elif providers.get(name, {}).get('disabled'):
                changed = True; providers[name] = {'provider': name, 'unavailable': True}
        out = _combine(providers)
    if not any(enabled.values()):
        return _combine({name: {'provider': name, 'disabled': True} for name in enabled})
    if (changed or not out or time.time() - max(out.get('attempt_at', 0), out.get('at', 0)) > TTL) and not _busy.is_set():
        _busy.set(); threading.Thread(target=_refresh, daemon=True).start()
    return out
