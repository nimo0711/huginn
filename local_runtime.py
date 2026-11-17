"""Read-only local process discovery; no session/port names are configured here."""
from __future__ import annotations
import concurrent.futures
import http.client
import os
from pathlib import Path
import plistlib
import re
import ssl
import shlex
import sqlite3
import json
import subprocess
import threading
import time
from urllib.parse import urlsplit, urlunsplit


def run(args, timeout=3):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def local_url(value):
    try:
        u = urlsplit(value.strip().rstrip('.,;)'))
        if u.scheme not in ('http', 'https') or u.hostname not in ('localhost','127.0.0.1','0.0.0.0','::1','::') or u.username or u.password:
            return None
        port = u.port
        if port is None or not 1 <= port <= 65535:
            return None
        # Paths are useful for viewers; never expose query credentials or fragments.
        host = '[::1]' if u.hostname in ('::1','::') else '127.0.0.1'
        return urlunsplit((u.scheme, f'{host}:{port}', u.path or '/', '', ''))
    except (ValueError, AttributeError):
        return None


def startup_urls(output):
    urls = []
    for line in str(output or '').splitlines():
        if not re.search(r'\bLocal:|\brunning (?:on|at)\b|\blistening (?:on|at)\b|\bserving (?:HTTP|at)\b|\bserver (?:started|ready)\b', line, re.I):
            continue
        for raw in re.findall(r'https?://[^\s\x1b<>"\']+', line):
            url = local_url(raw)
            if url and url not in urls:
                urls.append(url)
    return urls[:12]


def launch_command(command):
    try:
        lexer=shlex.shlex(str(command),posix=True,punctuation_chars=';&|');lexer.whitespace_split=True
        segments=[[]]
        for token in lexer:
            if token and all(c in ';&|' for c in token):segments.append([])
            else:segments[-1].append(token)
    except ValueError:return False
    readers={'cat','sed','rg','grep','head','tail','less','more','echo','printf','curl','wget','ls','pwd','find','read','cd','test','true','false'}
    for words in segments:
        while words and (re.match(r'\w+=',words[0]) or words[0] in ('env','nohup','exec','setsid')):words=words[1:]
        if words and os.path.basename(words[0]) not in readers:return True
    return False


def remember_start(task,output,started,completed):
    for url in startup_urls(output):
        prior=next((x for x in task['servers'] if x['url']==url),None)
        item={'url':url,'start_ts':started,'ts':completed}
        if prior:
            if completed>=prior['ts']:prior.update(item)
        else:task['servers'].append(item)


def processes():
    result = {}
    for line in run(['/bin/ps','-axo','pid=,ppid=,tty=,lstart=,comm=']).splitlines():
        parts = line.strip().split(None, 8)
        if len(parts) == 9 and parts[0].isdigit() and parts[1].isdigit():
            try:started=time.mktime(time.strptime(' '.join(parts[3:8]),'%a %b %d %H:%M:%S %Y'))
            except ValueError:started=0
            result[int(parts[0])] = {'ppid':int(parts[1]), 'tty':parts[2], 'comm':parts[8], 'started':started}
    return result


def listeners():
    result = []; pid = None; family = ''
    for line in run(['/usr/sbin/lsof','-nP','-iTCP','-sTCP:LISTEN','-Fptn'], 4).splitlines():
        if line.startswith('p') and line[1:].isdigit():
            pid = int(line[1:]);family = ''
        elif line.startswith('t'):family = line[1:]
        elif line.startswith('n') and pid:
            match = re.fullmatch(r'n(.+):(\d+)',line)
            if match:
                host=match[1].strip('[]')
                if host=='*':host='::1' if family=='IPv6' else '127.0.0.1'
                if host in ('0.0.0.0','localhost'):host='127.0.0.1'
                if host=='::':host='::1'
                if host not in ('127.0.0.1','::1'):continue
                row = {'pid':pid,'port':int(match[2]),'host':host}
                if row not in result: result.append(row)
    return result


def owner(pid, procs, roots):
    seen = set()
    while pid and pid not in seen:
        if pid in roots:return roots[pid]
        seen.add(pid);pid = procs.get(pid,{}).get('ppid',0)
    return None


def codex_roots(procs, ids):
    roots = {}
    for pid, proc in procs.items():
        if os.path.basename(proc['comm']) != 'codex-code-mode-host':continue
        # Only extract the public thread identifier; other environment values are
        # never logged, returned, or persisted.
        raw = run(['/bin/ps','eww','-p',str(pid),'-o','command='])
        match = re.search(r'(?:^|\s)CODEX_THREAD_ID=([0-9a-f-]{36})(?:\s|$)',raw)
        if match and match[1] in ids:roots[pid] = match[1]
    opened=codex_open_threads(procs)
    parents=codex_thread_roots({sid for values in opened.values() for sid in values})
    for pid,threads in opened.items():
        families={parents.get(sid) for sid in threads}
        if len(families)==1 and next(iter(families)) in ids:roots.setdefault(pid,next(iter(families)))
    return roots


def codex_open_threads(procs):
    pids=[str(pid) for pid,p in procs.items() if os.path.basename(p.get('comm',''))=='codex']
    if not pids:return {}
    found={};pid=0
    for line in run(['/usr/sbin/lsof','-p',','.join(pids),'-Fn'],4).splitlines():
        if line.startswith('p') and line[1:].isdigit():pid=int(line[1:])
        elif line.startswith('n') and '/sessions/' in line and 'rollout-' in line:
            match=re.search(r'([0-9a-f-]{36})\.jsonl$',line)
            if match:found.setdefault(pid,set()).add(match[1])
    return found


def codex_thread_roots(ids):
    found={}
    try:
        with sqlite3.connect('file:'+str(Path.home()/'.codex/state_5.sqlite')+'?mode=ro',uri=True,timeout=1) as conn:
            for sid in ids:
                current=sid;seen=set()
                while current not in seen and len(seen)<40:
                    seen.add(current);row=conn.execute('SELECT source FROM threads WHERE id=?',(current,)).fetchone()
                    if not row:break
                    try:source=json.loads(row[0])
                    except (ValueError,TypeError):source={}
                    parent=source.get('subagent',{}).get('thread_spawn',{}).get('parent_thread_id') if isinstance(source,dict) else None
                    if not parent:found[sid]=current;break
                    current=parent
    except (OSError,sqlite3.Error):pass
    return found


def terminal_for(pid, procs):
    seen = set()
    while pid and pid not in seen:
        seen.add(pid);proc = procs.get(pid,{})
        match = re.match(r'(.+?\.app)/Contents/',proc.get('comm',''))
        if match:
            path = Path(match[1])
            try:
                info = plistlib.loads((path/'Contents/Info.plist').read_bytes())
                return {'app':path.stem,'path':str(path),'bundle':info.get('CFBundleIdentifier',''),
                        'mode':'application','label':path.stem+' 열기',
                        'detail':'터미널 앱을 앞으로 가져옵니다. 탭 선택은 앱에서 해주세요.'}
            except (OSError, ValueError):return None
        pid = proc.get('ppid',0)
    return None


def codex_terminals(procs, ids):
    # A shared app server can hold several threads. Its open transcript proves
    # which terminal app can be opened, but never owns all of their servers.
    pids=[str(pid) for pid,p in procs.items() if os.path.basename(p.get('comm',''))=='codex']
    if not pids:return {}
    matches={};pid=0
    for line in run(['/usr/sbin/lsof','-p',','.join(pids),'-Fn'],4).splitlines():
        if line.startswith('p') and line[1:].isdigit():pid=int(line[1:])
        elif line.startswith('n') and '/sessions/' in line and 'rollout-' in line:
            match=re.search(r'([0-9a-f-]{36})\.jsonl$',line)
            if match and match[1] in ids:
                terminal=terminal_for(pid,procs)
                if terminal:matches.setdefault(match[1],{})[terminal['path']]=terminal
    return {sid:next(iter(apps.values())) for sid,apps in matches.items() if len(apps)==1}


def session_terminal(session,procs):
    terminal=terminal_for(session.get('pid',0),procs)
    if terminal or session.get('agent')!='codex':return terminal
    for pid in codex_roots(procs,{session['id']}):
        terminal=terminal_for(pid,procs)
        if terminal:return terminal
    return codex_terminals(procs,{session['id']}).get(session['id'])


def user_listener(row,procs):
    # Agent control endpoints also speak HTTP; they are not preview servers.
    name=os.path.basename(procs.get(row['pid'],{}).get('comm',''))
    return name not in ('claude','codex','codex-code-mode-host')


def output_session_owners(sessions, procs, ports):
    """Recover detached Claude servers from their *open writable* output logs.

    A port, working directory, launch timestamp or URL in a conversation is not
    ownership evidence. The live descriptor must point into a known session's
    scratchpad in this user's Claude temporary namespace. Never read log contents.
    """
    ids = {s['id'] for s in sessions if s.get('agent') == 'claude'}
    pids = {p['pid'] for p in ports if p['pid'] in procs and user_listener(p, procs)}
    if not ids or not pids:return {}
    uid = str(os.getuid());matches = {};pid = None;process_uid = '';fd = {};records = []
    # Query only stdout/stderr of live listeners, in one bounded subprocess.
    output = run(['/usr/sbin/lsof','-a','-p',','.join(map(str,sorted(pids))),
                  '-d','1,2','-Fpuftan'],4)
    for line in output.splitlines() + ['p']:
        field,value = line[:1],line[1:]
        if field in ('p','f'):
            if fd:records.append((pid,process_uid,fd))
            fd = {}
        if field == 'p':pid = int(value) if value.isdigit() else None;process_uid = ''
        elif field == 'u':process_uid = value
        elif field in ('f','a','t','n'):fd[field] = value
    for pid,process_uid,fd in records:
        if pid not in pids or process_uid != uid:continue
        if fd.get('f') not in ('1','2') or fd.get('a') not in ('w','u') or fd.get('t') != 'REG':continue
        path = fd.get('n','')
        # /tmp is macOS's alias for /private/tmp. Reject noncanonical path parts.
        if path.startswith('/tmp/'):path = '/private' + path
        parts = path.split('/')
        if '..' in parts or '.' in parts:continue
        if len(parts) < 8 or parts[:4] != ['', 'private', 'tmp', 'claude-'+uid]:continue
        sid = parts[5]
        if parts[4] and sid and parts[6] == 'scratchpad' and parts[-1]:
            matches.setdefault(pid,set()).add(sid)
    # Different session IDs on stdout/stderr are ambiguous, so do not guess.
    return {pid:next(iter(owners)) for pid,owners in matches.items()
            if len(owners) == 1 and owners <= ids}


def http_ready(url):
    u = urlsplit(url);conn = None
    try:
        if u.scheme == 'https':
            conn = http.client.HTTPSConnection(u.hostname,u.port,timeout=.4,context=ssl._create_unverified_context())
        else:conn = http.client.HTTPConnection(u.hostname,u.port,timeout=.4)
        conn.request('HEAD',u.path or '/')
        response = conn.getresponse()
        # A local listener returning any HTTP status is reachable. No redirects
        # are followed and no credentials or mutating requests are sent.
        return 100 <= response.status <= 599
    except (OSError, http.client.HTTPException, ValueError):return False
    finally:
        if conn:conn.close()


def resolve(sessions, procs, ports, extra_roots, output_owners=None):
    roots = dict(extra_roots)
    for s in sessions:
        if s.get('pid') in procs:roots[s['pid']] = s['id']
    result = {s['id']:{'servers':[], 'terminal':None} for s in sessions}
    candidates = {}
    for pid,sid in roots.items():
        if sid in result and not result[sid]['terminal']:
            result[sid]['terminal'] = terminal_for(pid,procs)
    ports=[row for row in ports if row['pid'] not in roots and user_listener(row,procs)]
    def server_owner(pid):
        return owner(pid,procs,roots) or (output_owners or {}).get(pid)
    for row in ports:
        sid = server_owner(row['pid'])
        if sid in result:
            host=row.get('host','127.0.0.1');authority='[::1]' if host=='::1' else host
            url = f"http://{authority}:{row['port']}/"
            evidence = '실행 프로세스' if owner(row['pid'],procs,roots) else '세션 실행 로그'
            candidates[(sid,host,row['port'])] = {'url':url,'port':row['port'],'evidence':evidence}
    live_ports = {(p.get('host','127.0.0.1'),p['port']):p['pid'] for p in ports}
    for s in sessions:
        for t in s.get('tasks',[]):
            for raw in t.get('servers',[]):
                url = local_url(raw.get('url','') if isinstance(raw,dict) else raw)
                if not url:continue
                parsed=urlsplit(url);port=parsed.port;binding=(parsed.hostname,port)
                if binding in live_ports:
                    pid=live_ports[binding];bound=server_owner(pid)
                    if bound and bound != s['id']:continue
                    if not bound:continue
                    candidates[(s['id'],parsed.hostname,port)] = {'url':url,'port':port,'evidence':'서버 시작 기록'}
    values = list(candidates.items())[:32]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ready = list(pool.map(lambda item:http_ready(item[1]['url']), values))
    for ((sid,_,_),item),ok in zip(values,ready):
        if ok:result[sid]['servers'].append(item)
    for item in result.values():item['servers'].sort(key=lambda row:row['port'])
    return result


_cache = {}; _at = 0; _busy = False; _lock = threading.Lock()

def refresh(sessions):
    global _cache,_at,_busy
    try:
        procs = processes()
        ports = listeners()
        found = resolve(sessions,procs,ports,codex_roots(procs,{s['id'] for s in sessions if s.get('agent')=='codex'}),
                        output_session_owners(sessions,procs,ports))
        for sid,terminal in codex_terminals(procs,{s['id'] for s in sessions if s.get('agent')=='codex'}).items():
            if sid in found and not found[sid]['terminal']:found[sid]['terminal']=terminal
        with _lock:_cache,_at = found,time.time()
    finally:
        with _lock:_busy = False


def attach(sessions):
    global _busy
    with _lock:
        stale = time.time()-_at > 10
        if stale and not _busy:
            _busy = True
            threading.Thread(target=refresh,args=(sessions,),daemon=True).start()
        for s in sessions:s['runtime'] = _cache.get(s['id'],{'servers':[],'terminal':None})
    return sessions
