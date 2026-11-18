from __future__ import annotations

import calendar
import copy
import glob
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import local_runtime
import threading
import time
import connections

import blocks
import enrich
import delegation
import progress
import title_summary

HOME = os.path.expanduser("~")
CLAUDE_SESSIONS = os.path.join(HOME, ".claude", "sessions")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_STATE = os.path.join(HOME, ".codex", "state_5.sqlite")
CODEX_HISTORY = os.path.join(HOME, ".codex", "thread_history_1.sqlite")
WINDOW_SEC = 12 * 3600
TASKS_KEPT = 40
TAG_RE = re.compile(r"<([a-zA-Z_-]+)[^>]*>.*?</\1[^>]*>", re.S)
PASTE_RE = re.compile(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", re.S)
CHANGE_RE = re.compile(
    r"\bgit\s+(-C\s+\S+\s+)?(commit|push|merge|rebase|reset|cherry-pick|revert|stash|checkout\s+-b|switch\s+-c)\b"
    r"|(^|[;&|]\s*)(rm|mv|cp|mkdir|ln|chmod|chown|tee|touch|kill|pkill)\s"
    r"|\bsed\s+-i|\b(pip|npm|brew|dnf|yum|apt)\s+(install|uninstall|remove)\b|\b(deploy|build)\.sh\b"
    r"|\balembic\s+(upgrade|downgrade|stamp|revision)\b|\b(launchctl|systemctl)\s+(kickstart|restart|start|stop|load|unload|enable|disable)\b"
    r"|\b(DROP|CREATE|INSERT|UPDATE|DELETE|ALTER|TRUNCATE)\s|\bglab\s+mr\s+(create|merge|update)|\bacli\b.*\b(create|transition|edit)\b"
    r"|\bswiftc\s.*-o\s|\bnpm\s+run\s+build|\bopen\s+-a|(^|[^0-9&>])>\s*(?!/dev/null|&)[\w~./$\"']",
    re.I,
)
CMD_LABELS = {
    "python3": "파이썬 실행", "python": "파이썬 실행", "pytest": "시험 실행", "uvicorn": "서버 기동",
    "curl": "API 호출", "grep": "검색", "rg": "검색", "find": "파일 찾기", "ls": "목록 보기",
    "cat": "파일 보기", "sed": "파일 보기·편집", "head": "파일 보기", "tail": "로그 보기",
    "psql": "DB 작업", "sqlite3": "DB 작업", "swiftc": "앱 빌드", "launchctl": "서비스 제어",
    "acli": "Jira 작업", "glab": "GitLab 작업", "scp": "파일 전송", "rsync": "파일 전송",
    "screencapture": "화면 캡처", "kill": "프로세스 종료", "pkill": "프로세스 종료",
}

READ_PROGS = {"grep", "rg", "cat", "head", "tail", "ls", "find", "wc", "echo", "printf", "which", "stat", "diff", "jq", "sort", "uniq", "awk", "less", "file", "du", "df", "ps", "pgrep", "lsof"}
READ_GIT = {"log", "show", "diff", "status", "branch", "rev-parse", "blame", "grep", "ls-files", "fetch", "remote"}

_cache: dict[str, tuple[int, dict]] = {}


def _is_change(cmd: str) -> bool:
    if re.search(r"open\([^)]*,\s*['\"][wa]b?['\"]|\.write_(text|bytes)\(", cmd or ""):
        return True
    for seg in re.split(r"&&|\|\||;|\n", cmd or ""):
        words = seg.strip().split()
        while words and ("=" in words[0] or words[0] in ("sudo", "env", "time", "nohup", "cd")):
            words = words[2:] if words[0] == "cd" else words[1:]
        if not words:
            continue
        prog = os.path.basename(words[0].lstrip("$("))
        rest = [w for w in words[1:] if not w.startswith("-")]
        if prog in READ_PROGS and not re.search(r"(^|[^0-9&>])>\s*(?!/dev/null|&)", seg):
            continue
        if prog == "git" and rest and (rest[1] if rest[0] == "-C" or words[1] == "-C" and len(rest) > 1 else rest[0]) in READ_GIT:
            continue
        if prog == "sed" and "-i" not in words:
            continue
        if CHANGE_RE.search(seg):
            return True
    return False


def _paste(m: re.Match) -> str:
    return "[붙여넣은 글: " + re.sub(r"\s+", " ", m.group(1)).strip()[:80] + "…] "


def _clean(text: str, limit: int = 240) -> str:
    text = PASTE_RE.sub(_paste, text or "")
    text = re.sub(r"\[Image #\d+\]", "[이미지]", text)
    text = re.sub(r"\s+", " ", TAG_RE.sub(" ", text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _para(text: str, limit: int = 2000) -> str:
    text = TAG_RE.sub("", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _short_dir(path: str) -> str:
    if not path:
        return ""
    path = path.replace(HOME, "~")
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) > 3 else path


def _iso(ts: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return 0.0


def _describe_cmd(cmd: str) -> str:
    cmd = re.sub(r"^/bin/\w+ -lc ", "", cmd or "").strip().strip("'\"")
    cmd = re.sub(r"^(cd \S+\s*&&\s*|export \S+\s*&&\s*)+", "", cmd)
    words = cmd.split()
    while words and (words[0].startswith("-") or "=" in words[0] or words[0] in ("sudo", "env", "time", "nohup")):
        words = words[1:]
    if not words:
        return "명령 실행"
    words[0] = words[0].lstrip("$(")
    prog = os.path.basename(words[0])
    if prog == "git" and len(words) > 3 and words[1] == "-C":
        words = [words[0]] + words[3:]
    if prog in ("git", "ssh") and len(words) > 1:
        target = words[1] if prog == "git" else next((w for w in words[1:] if not w.startswith("-")), "")
        return f"{'git' if prog == 'git' else '원격 접속'} {target.split('@')[-1]}"
    label = "파일 편집" if prog == "sed" and "-i" in words else CMD_LABELS.get(prog, prog)
    for w in words[1:6]:
        if re.search(r"\.(py|sh|sql|json|html|swift|ts|vue|md)$", w):
            return f"{label} · {os.path.basename(w)}"
    return label


def _request_excerpt(text: str) -> str:
    text = PASTE_RE.sub("", text or "")
    text = TAG_RE.sub("", text)
    if re.search(r"[━─]{3,}|(?m:^\s*\|)", text):
        prose = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not re.match(r"[━─]+|\|", ln.strip())]
        if prose:
            # A pasted table can be followed by an instruction on its last row.
            text = re.split(r"(?<=[.!?])\s+(?=[가-힣a-zA-Z])", prose[-1])[-1]
    clean = _clean(text, max(len(text), 240))
    return clean if len(clean) <= 240 else clean[:110] + " … " + clean[-110:]


def _new_task(prompt: str, ts: float) -> dict:
    return {
        "prompt": _clean(prompt, 400), "request": _request_excerpt(prompt), "extra": [], "docs": [], "plan_refs": [], "servers": [], "ts": ts, "start_ts": 0.0, "last_ts": ts, "end_ts": 0.0,
        "outcome": "", "notes": [], "checks": [], "steps": [], "files": [], "first_change": "",
        "n_checks": 0, "n_steps": 0, "areas": {}, "branch": "", "ask": "", "options": [],
    }


STORY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
PATH_RE = re.compile(r"(?:^|[\s'\"=(])((?:" + re.escape(HOME) + r"|~)/[^\s'\"`;|&)<>]+)")
IGNORED_DIRS = (".claude", ".codex", "Library", "Downloads", "Desktop", "Applications")


def _area(path: str) -> str:
    path = path.replace("~/", HOME + "/", 1)
    if not path.startswith(HOME + "/"):
        return ""
    parts = path[len(HOME) + 1:].split("/")
    if not parts[0] or parts[0] in IGNORED_DIRS or parts[0].startswith(".") or "node_modules" in parts:
        return ""
    if parts[0] == "vtm" and len(parts) > 2 and parts[1] in (".worktrees", "wt"):
        m = STORY_RE.search(parts[2])
        return m.group(1) if m else parts[2]
    if parts[0] == "vtm" and len(parts) > 3 and parts[1] == "문서":
        return f"문서/{parts[2]}/{parts[3]}"
    if parts[0] == "vtm" and len(parts) > 2:
        return parts[1]
    if parts[0] == "vtm" or (len(parts) < 2 and not os.path.isdir(path)):
        return ""
    return parts[0]


def _touch(task: dict, paths: list, weight: int) -> None:
    for p in paths:
        a = _area(p or "")
        if a:
            task["areas"][a] = task["areas"].get(a, 0) + weight


def _cmd_paths(cmd: str, cwd: str = "") -> list:
    found = [m.group(1) for m in PATH_RE.finditer(cmd or "")]
    found += re.findall(r"\bcd\s+(/\S+)", cmd or "")
    if cwd:
        found.append(cwd)
    return found


def _record(task: dict, ts: float, change: bool, text: str, described: bool, file: str = "") -> None:
    task["last_ts"] = ts
    if change and not task["start_ts"]:
        task["start_ts"] = ts
        task["first_change"] = text
    if file and change and file not in task["files"]:
        task["files"].append(file)
    if change:
        task["n_steps"] += 1
        if described:
            task["steps"].append({"ts": ts, "text": text})
            del task["steps"][:-15]
    else:
        task["n_checks"] += 1
        if described:
            task["checks"].append({"ts": ts, "text": text})
            del task["checks"][:-12]


def _note(task: dict, ts: float, text: str) -> None:
    text = _para(text)
    if not text:
        return
    task["last_ts"] = ts
    task["notes"].append({"ts": ts, "stage": "진행" if task["start_ts"] else "계획", "text": text})
    del task["notes"][:-25]


def _close(task: dict, ts: float, outcome: str = "") -> None:
    if task["end_ts"]:
        return
    task["end_ts"] = ts or task["last_ts"]
    task["outcome"] = outcome or ("완료" if task["start_ts"] else "답변")


def _claude_action(block: dict) -> tuple[bool, str, bool, str]:
    name = block.get("name") or ""
    inp = block.get("input") or {}
    if name == "Bash":
        cmd = inp.get("command") or ""
        desc = inp.get("description") or ""
        return _is_change(cmd), desc or _describe_cmd(cmd), bool(desc), ""
    if name in ("Edit", "Write", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        return True, f"{os.path.basename(path)} {'작성' if name == 'Write' else '수정'}", True, os.path.basename(path)
    if name == "Read":
        return False, f"{os.path.basename(inp.get('file_path') or '')} 읽기", True, ""
    if name in ("Agent", "Task"):
        return False, f"하위 에이전트에 맡김 · {inp.get('description') or ''}", True, ""
    if name in ("WebSearch", "WebFetch"):
        return False, f"웹 조사 · {inp.get('query') or inp.get('url') or ''}", True, ""
    if name == "Skill":
        return False, f"스킬 사용 · {inp.get('skill') or ''}", True, ""
    if name == "AskUserQuestion":
        return False, "사용자에게 질문", True, ""
    return False, name, False, ""


def _user_text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _new_info() -> dict:
    return {"tasks": [], "cwd": "", "branch": "", "entry": "", "last_ts": 0.0, "model": "", "context": 0, "tool": None, "options": [], "last_tool": None, "recap": None, "compacts": 0, "fails": [], "out_tokens": 0, "msg_ids": set(), "tool_text": {}, "pending_docs": {}}


def _open_task(info: dict, prompt: str, ts: float) -> None:
    tasks = info["tasks"]
    if tasks:
        _close(tasks[-1], tasks[-1]["last_ts"])
    task = _new_task(prompt, ts)
    task['branch'] = info.get('branch', '')
    tasks.append(task)
    recent = [t for t in tasks if not t.get('history')]
    if len(recent) > TASKS_KEPT:
        history = [t for t in tasks if t.get('history')]
        for old in recent[:-TASKS_KEPT]:
            _archive_task(history, old, info.get('branch', ''))
        tasks[:] = sorted(history, key=lambda t: t['last_ts']) + recent[-TASKS_KEPT:]


def _task_progress(task):
    return next((p for path in reversed(task.get('plan_refs', []))
                 if (p := progress.read_plan(path, DOC_ROOT)) and p.get('ts', 0) >= task['ts']), None)


def _archive_task(history, task, branch=''):
    """Keep counts and unresolved evidence per work, never old transcript bodies."""
    previous = max(history, key=lambda t: t['last_ts']).get('history_key', '') if history else ''
    key, _, _ = _work_key(task, branch, previous)
    row = next((t for t in history if t.get('history_key') == key), None)
    if row is None:
        row = _new_task(task.get('request') or task['prompt'], task['ts'])
        row.update(history_key=key, branch=task.get('branch') or branch,
                   history={'total': 0, 'done': 0, 'plans': {}})
        history.append(row)
    count = 1 + len(task.get('extra', []))
    row['history']['total'] += count
    row['n_steps'] += task['n_steps']
    row['n_checks'] += task['n_checks']
    row['start_ts'] = row['start_ts'] or task['start_ts']
    row['last_ts'] = max(row['last_ts'], task['last_ts'])
    row['end_ts'] = max(row['end_ts'], task['end_ts'])
    row['outcome'] = '완료'
    completed = bool(task['end_ts'] and (task.get('result') or task['notes'])
                     and task['outcome'] in ('완료', '답변'))
    if completed:
        plan = _task_progress(task)
        if plan and any(i['state'] != 'done' for i in plan['items']):
            path = str(Path(DOC_ROOT) / plan['rel'])
            pending = row['history']['plans'].setdefault(path, {'count': 0, 'ts': task['ts']})
            pending['count'] += count
            pending['ts'] = max(pending['ts'], task['ts'])
        else:
            row['history']['done'] += count


def _feed(info: dict, line: str) -> None:
    try:
        e = json.loads(line)
    except ValueError:
        return
    if not isinstance(e, dict) or e.get("isSidechain"):
        return
    ts = _iso(e.get("timestamp") or "")
    if ts:
        info["last_ts"] = max(info["last_ts"], ts)
    if e.get("cwd"):
        info["cwd"] = e["cwd"]
    if e.get("gitBranch"):
        info["branch"] = e["gitBranch"]
    if e.get("entrypoint"):
        info["entry"] = e["entrypoint"]
    tasks = info["tasks"]
    current = tasks[-1] if tasks and not tasks[-1]["end_ts"] else None
    kind = e.get("type")
    if kind == "attachment":
        att = e.get("attachment") or {}
        if att.get("type") == "queued_command" and (att.get("origin") or {}).get("kind") == "human":
            raw = att.get("prompt")
            if isinstance(raw, list):
                raw = " ".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text")
            if isinstance(raw, str) and raw.strip():
                if current:
                    current["extra"].append({"ts": ts, "text": _clean(raw, 400), "request": _request_excerpt(raw)})
                    current["last_ts"] = ts
                else:
                    _open_task(info, raw, ts)
        return
    if kind == "system":
        sub = e.get("subtype")
        if sub == "away_summary" and isinstance(e.get("content"), str):
            info["recap"] = {"ts": ts, "text": e["content"].strip()[:600]}
        elif sub == "compact_boundary":
            info["compacts"] += 1
        return
    if kind == "user":
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tid = b.get("tool_use_id") or ""
                server_start=info.setdefault('pending_servers',{}).pop(tid,None)
                if server_start and not b.get('is_error'):
                    local_runtime.remember_start(server_start[0],blocks.result_text(b.get('content')),server_start[1],ts)
                if current and tid == current.get("ask_tool_id") and not b.get("is_error"):
                    current["ask"] = ""
                    current["options"] = []
                    current.pop("ask_tool_id", None)
                pend = info["pending_docs"].pop(tid, None)
                if pend and not b.get("is_error"):
                    _confirm_docs(pend, ts)
                plan = info.setdefault("pending_plans", {}).pop(tid, None)
                if plan and not b.get("is_error"):
                    _plan_reference(*plan)
                if info["tool"] and tid == info["tool"]["id"]:
                    kind = blocks.block_kind(b.get("content")) if b.get("is_error") else ""
                    info["last_tool"] = dict(info["tool"], end=ts, failed=bool(b.get("is_error")) and not kind, blocked=kind)
                    info["tool"] = None
                if b.get("is_error"):
                    body = re.sub(r"</?tool_use_error>", "", blocks.result_text(b.get("content")))
                    blocked = blocks.is_blocked(body)
                    first = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("Exit code")), "")
                    info["fails"].append({"ts": ts, "what": info["tool_text"].get(tid, ""), "why": _clean(first, 160), "blocked": blocked})
                    del info["fails"][:-20]
                    if current and not blocked:
                        current["fails"] = current.get("fails", 0) + 1
    if kind == "user" and not e.get("isMeta"):
        text = _user_text(e)
        if not text:
            return
        if text.startswith("[Request interrupted"):
            if current:
                _close(current, ts, "중단")
            return
        if text.startswith(("<command-", "<local-command", "Caveat:", "<task-notification", "This session is being continued", "<system-reminder")):
            return
        prompt = _clean(text, 400)
        if prompt:
            _open_task(info, text, ts)
        return
    if kind == "assistant":
        msg = e.get("message") or {}
        if msg.get("model") and not str(msg["model"]).startswith("<"):
            info["model"] = msg["model"]
        u = msg.get("usage") or {}
        ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
        if ctx:
            info["context"] = ctx
        mid = msg.get("id")
        if mid and mid not in info["msg_ids"]:
            info["msg_ids"].add(mid)
            info["out_tokens"] += u.get("output_tokens") or 0
    if kind != "assistant" or not current:
        return
    for b in (e.get("message") or {}).get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            if b.get("name") == "AskUserQuestion":
                qs = (b.get("input") or {}).get("questions") or []
                current["ask"] = " / ".join(q.get("question", "") for q in qs if isinstance(q, dict))[:400]
                current["options"] = [o.get("label", "") for q in qs if isinstance(q, dict) for o in (q.get("options") or []) if isinstance(o, dict)][:8]
                current["ask_tool_id"] = b.get("id") or ""
            change, text, described, file = _claude_action(b)
            info["tool"] = {"id": b.get("id") or "", "name": b.get("name") or "", "text": _clean(text, 120), "ts": ts}
            info["tool_text"][b.get("id") or ""] = _clean(text, 120)
            if len(info["tool_text"]) > 200:
                info["tool_text"] = dict(list(info["tool_text"].items())[-100:])
            _record(current, ts, change, _clean(text, 160), described, file)
            inp = b.get("input") or {}
            if b.get('name')=='Bash' and local_runtime.launch_command(inp.get('command','')):
                info.setdefault('pending_servers',{})[b.get('id') or '']=(current,ts)
            if b.get("name") in ("Write", "Edit") or b.get("name") == "Bash" and change:
                info.setdefault("pending_plans", {})[b.get("id") or ""] = (current, [inp.get("file_path")] + _cmd_paths(inp.get("command") or ""))
            _touch(current, [inp.get("file_path"), inp.get("notebook_path"), inp.get("path")] + _cmd_paths(inp.get("command") or ""), 3 if change else 1)
            if b.get("name") == "Write":
                _touch(current, _cmd_paths(inp.get("content") or ""), 3)
            if b.get("name") in ("Write", "Edit", "NotebookEdit"):
                _doc(current, inp.get("file_path") or inp.get("notebook_path") or "", ts, b.get("name") == "Write")
            elif b.get("name") == "Bash":
                cands = set(_doc_paths(inp.get("command") or "")) | set(current.get("script_docs") or ())
                if cands:
                    info["pending_docs"][b.get("id") or ""] = (current, cands, ts)
                    if len(info["pending_docs"]) > 50:
                        info["pending_docs"] = dict(list(info["pending_docs"].items())[-25:])
            if b.get("name") == "Write":
                found = _doc_paths(inp.get("content") or "")
                if found:
                    current["script_docs"] = sorted(set(current.get("script_docs") or ()) | set(found))[-20:]
            if info["branch"]:
                current["branch"] = info["branch"]
        elif b.get("type") == "text" and b.get("text", "").strip():
            _note(current, ts, b["text"])


DOC_EXT = {".html", ".htm", ".md", ".pdf", ".xlsx", ".docx", ".pptx", ".png", ".svg"}
DOC_ROOT = os.path.realpath(os.path.expanduser("~/vtm/문서"))


def _plan_reference(task: dict, paths: list) -> None:
    for path in paths:
        if not path or not path.endswith('.md') or '/계획서/' not in path:
            continue
        real = os.path.realpath(os.path.expanduser(path))
        if not real.startswith(DOC_ROOT + os.sep):
            continue
        refs = task.setdefault('plan_refs', [])
        if real in refs:
            refs.remove(real)
        refs.append(real)
        del refs[:-8]


def _doc(task: dict, path: str, ts: float, created: bool) -> None:
    if not path:
        return
    real = os.path.realpath(path)
    if not real.startswith(DOC_ROOT + os.sep):
        return
    docs = task.setdefault("docs", [])
    for d in docs:
        if d["path"] == real:
            d["ts"] = ts
            return
    docs.append({"path": real, "rel": os.path.relpath(real, DOC_ROOT), "ts": ts, "created": created})


def _doc_paths(text: str) -> list:
    return [p for p in _cmd_paths(text) if os.path.splitext(p)[1].lower() in DOC_EXT and os.path.realpath(p).startswith(DOC_ROOT + os.sep)]


def _confirm_docs(pend: tuple, end: float) -> None:
    task, paths, start = pend
    for p in paths:
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if start - 2 <= m <= end + 2:
            _doc(task, p, start, False)


def _live_docs(tasks: list) -> None:
    for t in tasks:
        t["docs"] = [d for d in t.get("docs") or [] if os.path.exists(d["path"])]


def _parse_claude(path: str) -> dict:
    size = os.path.getsize(path)
    offset, info = _cache.get(path, (0, None))
    if info is None or size < offset:
        offset, info = 0, _new_info()
    if size > offset:
        with open(path, "rb") as f:
            f.seek(offset)
            for line in f:
                if not line.endswith(b'\n'):
                    break
                _feed(info, line.decode('utf-8', 'replace'))
                offset = f.tell()
    _cache[path] = (offset, info)
    return info


ASK_RE = re.compile(
    r"(?:할까요|될까요|괜찮을까요|하시겠습니까|하시겠어요|인가요|입니까)\s*[?.!？]?\s*$"
    r"|(?:해|알려|정해|골라|선택해|확인해|승인해|허용해)\s*주(?:세요|십시오)[.!?？]?(?:\s|$)"
    r"|(?:승인|허락|허용|응답)(?:이|가|을|를)?\s*(?:필요합니다|기다리고 있습니다|기다립니다|부탁드립니다)"
    r"|\b(?:can|could|would|will)\s+you\b|\bplease\s+(?:confirm|choose|approve|provide|select)\b",
    re.I,
)


def _question(text: str) -> str:
    # Syntax in links, code and quoted examples is not a request to the user.
    text = re.sub(r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)", "", text or "")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    for ln in reversed(lines[-3:]):
        if ln.startswith((">", "|")):
            continue
        plain = re.sub(r"`[^`]*`", "", ln)
        plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", plain)
        plain = re.sub(r"https?://[^\s<>]+", "", plain)
        plain = re.sub(r"[*`#]", "", plain).strip()
        question = re.search(r"[가-힣a-zA-Z][^\n]*[?？]\s*$", plain)
        if ASK_RE.search(plain) or question:
            return re.sub(r"[*`#]", "", ln).strip()[:300]
    return ""


def _finish(tasks: list[dict], running: bool, now: float) -> list[dict]:
    out = []
    for i, t in enumerate(tasks):
        t = dict(t, notes=list(t["notes"]))
        if i == len(tasks) - 1 and not t["end_ts"]:
            if running:
                t["outcome"] = "진행" if t["start_ts"] else "계획"
            else:
                t["end_ts"] = t["last_ts"]
                t["outcome"] = "완료" if t["start_ts"] else "답변"
        t["result"] = ""
        if t["outcome"] in ("완료", "답변", "중단") and t["notes"]:
            t["result"] = t["notes"].pop()["text"]
        t["now"] = next((text for n in reversed(t["notes"]) if (text := _activity_text(n["text"]))), "") if t["outcome"] in ("계획", "진행") else ""
        t["progress"] = _task_progress(t)
        if t.get('history'):
            history = dict(t['history'])
            history['done'] += sum(v['count'] for path, v in history['plans'].items()
                                   if (p := progress.read_plan(path, DOC_ROOT)) and p.get('ts', 0) >= v['ts']
                                   and all(item['state'] == 'done' for item in p['items']))
            t['history'] = history
        if i == len(tasks) - 1 and not running:
            t["ask"] = t.get("ask") or _question(t["result"])
        else:
            t["ask"] = ""
        out.append(t)
    return out


def _activity_text(text: str) -> str:
    out = []
    question = False
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        listed = re.match(r'^\s*(?:\d+[.)]|[-*]|\|)\s*', line)
        if question and listed:
            continue
        parts = [line] if listed else re.split(r'(?<=[.!?？])\s+(?=[가-힣A-Za-z#*])', line)
        for part in parts:
            question = bool(_question(part))
            if not question:
                out.append(part)
    return '\n'.join(out)


BOARD_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.db")


JIRA_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jira_titles.json")
_jira: dict = {}
_jira_tried: dict = {}
_jira_lock = threading.Lock()


def _load_jira() -> None:
    try:
        with open(JIRA_CACHE) as f:
            _jira.update(json.load(f))
    except (OSError, ValueError):
        pass


def _fetch_jira(key: str) -> None:
    try:
        out = subprocess.run(
            [connections.cli('acli'), "jira", "workitem", "view", key, "--fields", "summary", "--json"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        title = (json.loads(out).get("fields") or {}).get("summary") or ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    if not title:
        return
    title = re.sub(r"^(\s*\[[^\]]*\])+\s*", "", title).strip()
    with _jira_lock:
        _jira[key] = title
        try:
            with open(JIRA_CACHE, "w") as f:
                json.dump(_jira, f, ensure_ascii=False)
        except OSError:
            pass


def _jira_title(key: str) -> str:
    return (enrich.story(key).get('title') or '') if connections.STORE.get('jira')['enabled'] else ''


_load_jira()


def _story_titles() -> dict:
    titles: dict = {}
    try:
        con = sqlite3.connect(BOARD_DB, timeout=1)
        rows = con.execute("SELECT story, title FROM events WHERE kind = 'story' AND title != '' ORDER BY ts").fetchall()
        con.close()
        titles = {k: re.sub(r"^(\s*\[[^\]]*\])+\s*", "", t) for k, t in rows}
    except sqlite3.Error:
        pass
    return titles


def _work_key(task, session_branch='', prev=''):
    key = task.get('history_key', '')
    branch = STORY_RE.search(task.get('branch') or session_branch or '')
    if not key:
        if branch:
            key = branch.group(1)
        elif task['areas']:
            key = max(task['areas'].items(), key=lambda kv: kv[1])[0]
        else:
            key = prev
    raw, auto = key, False
    if ALIASES.get(key) and ALIASES[key] != key:
        key = ALIASES[key]
    elif key.startswith('문서/') and ALIASES.get(key) != key:
        explicit = re.search(r'(?<![A-Z0-9])([A-Z][A-Z0-9]+-\d+)(?!\d)', key)
        if explicit:
            key, auto = explicit.group(1), True
        elif prev and not STORY_RE.fullmatch(prev) and not prev.startswith('문서/'):
            key, auto = prev, True
    return key, raw, auto


def _works(tasks: list, session_branch: str, titles: dict) -> list:
    works: list = []
    index: dict = {}
    prev = ""
    for i, t in enumerate(tasks):
        key, raw, auto = _work_key(t, session_branch, prev)
        prev = key or prev
        if key not in index:
            index[key] = len(works)
            story = key if STORY_RE.fullmatch(key or "") else ""
            title = (_jira_title(story) if story else '') or titles.get(key, "")
            works.append({"key": key, "story": story, "title": title, "tasks": [], "merged": []})
        works[index[key]]["tasks"].append(i)
        if raw != key and not any(x["key"] == raw for x in works[index[key]]["merged"]):
            works[index[key]]["merged"].append({"key": raw, "auto": auto})
    for w in works:
        if not w["key"]:
            w["key"] = "기타 대화"
    for w in works:
        w.setdefault("merged", [])
        if w["story"]:
            info = enrich.story(w["story"])
            w["info"] = info
            w["unit"] = enrich.work_unit(w["story"], info)
            w["stage"] = enrich.stage(info)
            if info.get("title"):
                w["title"] = info["title"]
        elif not w["title"]:
            turns = []
            for i in w["tasks"]:
                turns.append({"request": tasks[i].get("request") or tasks[i]["prompt"],
                              "followups": [e.get("request") or _request_excerpt(e["text"])
                                            for e in tasks[i].get("extra", [])][-8:]})
            if any(t["request"] for t in turns):
                context = {"area": w["key"], "objective": next(t["request"] for t in turns if t["request"]),
                           "turns": turns if len(turns) <= 8 else turns[:2] + turns[-6:]}
                summary = title_summary.get(context)
                w["title"] = summary["title"] or ("작업 제목 정리 중" if summary["state"] == "pending" else "작업 제목 미확인")
                w["title_source"] = "summary"
                w["title_state"] = summary["state"]
    works.sort(key=lambda w: w["tasks"][-1])
    return works


def claude_sessions(now: float) -> list[dict]:
    live: dict[str, dict] = {}
    for f in glob.glob(os.path.join(CLAUDE_SESSIONS, "*.json")):
        try:
            with open(f) as fh:
                d = json.JSONDecoder().raw_decode(fh.read().lstrip())[0]
        except (OSError, ValueError):
            continue
        if d.get("sessionId") and _alive(int(d.get("pid") or 0)):
            live[d["sessionId"]] = d
    out = []
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        sid = os.path.splitext(os.path.basename(path))[0]
        mtime = os.path.getmtime(path)
        if sid not in live and now - mtime > WINDOW_SEC:
            continue
        info = _parse_claude(path)
        if info["entry"].startswith("sdk") or not info["tasks"]:
            continue
        meta = live.get(sid)
        wait = None
        if meta:
            running = meta.get("status") not in ("idle", "waiting", None, "")
            tasks_done = _finish(info["tasks"], running, now)
            wait = _wait_reason(meta, tasks_done[-1], info["last_tool"], running)
            state = "작업 중" if running and not wait else "입력 대기" if wait else "쉬는 중"
        else:
            running = now - mtime < 120
            state = "작업 중" if running else "종료됨"
        _live_docs(info["tasks"])
        out.append({
            "agent": "claude",
            "pid": int((meta or {}).get("pid") or 0),
            "cwd_path": info["cwd"] or (meta or {}).get("cwd", ""),
            "id": sid,
            "name": (meta or {}).get("name") or "",
            "state": state,
            "cwd": _short_dir(info["cwd"] or (meta or {}).get("cwd", "")),
            "branch": info["branch"] if info["branch"] != "HEAD" else "",
            "tasks": tasks_done if meta else _finish(info["tasks"], running, now),
            "works": _works(info["tasks"], info["branch"], TITLES),
            "last_ts": max(mtime, info["last_ts"]),
            "model": info["model"],
            "context": info["context"],
            "tool": info["tool"] if running else None,
            "last_tool": info["last_tool"],
            "wait": wait,
            "recap": info["recap"],
            "compacts": info["compacts"],
            "fails": [f for f in info["fails"] if now - f["ts"] < 3600][-5:],
            "out_tokens": info["out_tokens"],
            "agents": _subagents(path[:-6], now),
        })
    return out


WAIT_TEXT = {"input needed": "입력 요청", "sandbox request": "샌드박스 허용 요청", "dialog open": "열린 창이 응답을 기다림", "goal proposal": "목표 제안 확인", "worker request": "작업자 요청 확인"}


def _wait_reason(meta: dict, task: dict, last_tool: dict | None, running: bool) -> dict | None:
    at = (meta.get("statusUpdatedAt") or 0) / 1000
    raw = (meta.get("waitingFor") or "").strip() if meta.get("status") == "waiting" else ""
    if raw == "approve plan":
        return {"kind": "plan", "text": "계획 승인", "ts": at}
    if raw.startswith("approve "):
        return {"kind": "perm", "text": _clean(raw[len("approve "):], 160), "ts": at}
    if task.get("ask") and not running and raw in ("", "input needed"):
        return {"kind": "ask", "text": task["ask"], "ts": task.get("end_ts") or task.get("last_ts") or at}
    if raw:
        return {"kind": "other", "text": WAIT_TEXT.get(raw, _clean(raw, 120)), "ts": at}
    if not running and last_tool and last_tool.get("blocked") == "auto":
        return {"kind": "auto", "text": last_tool.get("text") or last_tool.get("name") or "", "ts": last_tool.get("end") or at}
    return None


def _subagents(base: str, now: float) -> dict:
    running, done = [], 0
    for meta_path in glob.glob(os.path.join(base, "subagents", "*.meta.json")):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        log = meta_path[:-len(".meta.json")] + ".jsonl"
        try:
            mtime = os.path.getmtime(log)
            born = os.path.getmtime(meta_path)
        except OSError:
            continue
        if now - mtime > WINDOW_SEC:
            continue
        if meta.get("finished") or now - mtime > 900:
            done += 1
        else:
            running.append({"what": _clean(meta.get("description") or meta.get("agentType") or "", 80), "ts": born, "last": mtime})
    running.sort(key=lambda a: a["ts"])
    return {"running": running, "done": done}


def _codex_turn(hist: sqlite3.Connection, tid: str, turn_id: str, status: str, started: float, completed: float) -> dict:
    columns={r[1] for r in hist.execute('PRAGMA table_info(thread_items)')}
    timing=', started_at_ms, completed_at_ms' if {'started_at_ms','completed_at_ms'}<=columns else ', NULL, NULL'
    rows = hist.execute(
        "SELECT item_type, item_json, created_at_ms" + timing + " FROM thread_items WHERE thread_id = ? AND turn_id = ? "
        "AND item_type IN ('userMessage','agentMessage','commandExecution','fileChange','webSearch','collabAgentToolCall') "
        "ORDER BY rollout_ordinal",
        (tid, turn_id),
    ).fetchall()
    task = None
    for kind, raw, created, command_start, command_end in rows:
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        ts = (created or 0) / 1000 or started
        if kind == "userMessage":
            text = " ".join(c.get("text", "") for c in d.get("content") or [] if isinstance(c, dict))
            if task is None:
                task = _new_task(text, ts)
            elif text:
                task["extra"].append({"ts": ts, "text": _clean(text, 400), "request": _request_excerpt(text)})
            continue
        if task is None:
            task = _new_task("", started)
        if kind == "agentMessage":
            _note(task, ts, d.get("text") or "")
        elif kind == "commandExecution":
            cmd = d.get("command") or ""
            if d.get("exitCode") in (None,0) and local_runtime.launch_command(cmd):
                start=command_start/1000 if command_start else ts
                end=command_end/1000 if command_end else ts
                local_runtime.remember_start(task,d.get('aggregatedOutput') or d.get('output') or '',start,end)
            change = _is_change(cmd)
            _record(task, ts, change, _describe_cmd(cmd), False)
            _touch(task, _cmd_paths(cmd, d.get("cwd") or ""), 3 if change else 1)
            if change and d.get("exitCode") == 0:
                _plan_reference(task, _cmd_paths(cmd, d.get("cwd") or ""))
        elif kind == "fileChange":
            for c in d.get("changes") or []:
                if isinstance(c, dict):
                    name = os.path.basename(c.get("path") or "")
                    _record(task, ts, True, f"{name} 수정", True, name)
                    _touch(task, [c.get("path") or ""], 3)
                    if d.get("status") == "completed":
                        _plan_reference(task, [c.get("path") or ""])
        elif kind == "webSearch":
            _record(task, ts, False, "웹 조사 · " + _clean(d.get("query") or "", 80), True)
        elif kind == "collabAgentToolCall":
            _record(task, ts, False, "하위 에이전트에 맡김", True)
    if task is None:
        task = _new_task("", started)
    if status != "inProgress":
        _close(task, completed or task["last_ts"], {"interrupted": "중단", "failed": "실패"}.get(status, ""))
    return task


_codex_past = {}
_codex_recent = {}


def _recent_codex_tasks(hist, tid, turns, identity):
    columns = {row[1] for row in hist.execute('PRAGMA table_info(thread_items)')}
    if 'updated_at_ordinal' not in columns:
        # Older databases cannot prove that an item was not edited in place.
        _codex_recent.pop(identity, None)
        return [_codex_turn(hist, tid, turn, status, float(started or 0), float(completed or 0))
                for turn, status, started, completed, _ in reversed(turns)]
    previous = _codex_recent.get(identity, {})
    current = {}
    recent = []
    for turn, status, started, completed, ordinal in reversed(turns):
        revision = hist.execute(
            'SELECT count(*), max(rollout_ordinal), max(updated_at_ordinal) FROM thread_items '
            'WHERE thread_id=? AND turn_id=?', (tid, turn)).fetchone()
        signature = (status, started, completed, ordinal, revision)
        saved = previous.get(turn)
        if saved is None or saved[0] != signature:
            task = _codex_turn(hist, tid, turn, status, float(started or 0), float(completed or 0))
            saved = (signature, task)
        current[turn] = saved
        # Response decoration and consumers must never change the cached source.
        recent.append(copy.deepcopy(saved[1]))
    _codex_recent[identity] = current
    return recent


def _codex_tasks(hist, tid, turns, branch, database_identity):
    """Parse recent turn bodies; aggregate older turns once per database lifetime."""
    oldest = turns[-1][4]
    identity = (*database_identity, tid, branch)
    for cache in (_codex_past, _codex_recent):
        for old_identity in list(cache):
            if old_identity[2] == tid and old_identity != identity:
                cache.pop(old_identity, None)
    saved = _codex_past.get(identity)
    old_count = hist.execute('SELECT count(*) FROM thread_turns WHERE thread_id=? AND rollout_ordinal<?',
                             (tid, oldest)).fetchone()[0]
    if not saved or saved['through'] >= oldest or old_count < saved['count'] or saved.get('unfinished'):
        saved = {'through': -1, 'count': 0, 'history': [], 'unfinished': False}
        _codex_past[identity] = saved
    rows = hist.execute('SELECT turn_id,status,started_at,completed_at,rollout_ordinal FROM thread_turns '
                        'WHERE thread_id=? AND rollout_ordinal<? AND rollout_ordinal>? ORDER BY rollout_ordinal',
                        (tid, oldest, saved['through']))
    for turn_id, status, started, completed, ordinal in rows:
        task = _codex_turn(hist, tid, turn_id, status, float(started or 0), float(completed or 0))
        if task['prompt'] or task['notes'] or task['n_steps'] or task['n_checks']:
            _archive_task(saved['history'], task, branch)
        saved['through'] = ordinal
        saved['count'] += 1
        saved['unfinished'] |= status == 'inProgress'
    recent = _recent_codex_tasks(hist, tid, turns, identity)
    return sorted(saved['history'], key=lambda t: t['last_ts']) + recent


def codex_sessions(now: float) -> list[dict]:
    if not os.path.exists(CODEX_STATE):
        return []
    st = hist = None
    try:
        database_identity = (os.path.realpath(CODEX_HISTORY), os.stat(CODEX_HISTORY).st_ino)
        st = sqlite3.connect(Path(CODEX_STATE).as_uri() + '?mode=ro', uri=True, timeout=1)
        hist = sqlite3.connect(Path(CODEX_HISTORY).as_uri() + '?mode=ro', uri=True, timeout=1)
        if database_identity != (os.path.realpath(CODEX_HISTORY), os.stat(CODEX_HISTORY).st_ino):
            raise sqlite3.OperationalError('History database replaced while opening')
        # The open handle retains this identity even if its path is replaced later.
        rows = st.execute(
            "SELECT id, updated_at, cwd, title, git_branch, source, created_at FROM threads "
            "WHERE archived = 0 AND updated_at > ? ORDER BY updated_at DESC LIMIT 40",
            (int(now - WINDOW_SEC),),
        ).fetchall()
    except (OSError, sqlite3.Error):
        if hist is not None:
            hist.close()
        if st is not None:
            st.close()
        return []
    live_ids = {row[0] for row in rows}
    for cache in (_codex_past, _codex_recent):
        for identity in list(cache):
            if identity[2] not in live_ids:
                cache.pop(identity, None)
    out = []
    for tid, updated, cwd, title, branch, source, created in rows:
        if "subagent" in str(source or ""):
            continue
        try:
            turns = hist.execute(
                "SELECT turn_id, status, started_at, completed_at, rollout_ordinal FROM thread_turns WHERE thread_id = ? "
                "ORDER BY rollout_ordinal DESC LIMIT ?",
                (tid, TASKS_KEPT),
            ).fetchall()
            if not turns:
                for cache in (_codex_past, _codex_recent):
                    for identity in list(cache):
                        if identity[2] == tid:
                            cache.pop(identity, None)
                continue
            tasks = _codex_tasks(hist, tid, turns, branch or '', database_identity)
        except sqlite3.Error:
            continue
        tasks = [t for t in tasks if t.get('history') or t["prompt"] or t["notes"] or t["n_steps"] or t["n_checks"]]
        if not tasks:
            continue
        if not tasks[0]["prompt"]:
            tasks[0]["prompt"] = _clean(title or "", 400)
        running = any(s == "inProgress" for _, s, _, _, _ in turns[:1])
        done = _finish(tasks, running, now)
        idle_state = "입력 대기" if done[-1].get("ask") else "쉬는 중"
        out.append({
            "agent": "codex",
            "cwd_path": cwd or "",
            "created_at": float(created or 0),
            "role": "외부 평가" if re.search(r'검토|평가|\breview\b|\baudit\b', title or '', re.I) else "위임 작업",
            "id": tid,
            "name": "Claude가 부른 Codex" if source == "exec" else "",
            "state": "작업 중" if running else (idle_state if now - updated < 1800 else "종료됨"),
            "cwd": _short_dir(cwd or ""),
            "branch": branch or "",
            "tasks": done,
            "works": _works(tasks, branch or "", TITLES),
            "last_ts": float(updated),
        })
    st.close()
    hist.close()
    return out


TITLES: dict = {}
ALIASES: dict = {}


def _aliases() -> dict:
    try:
        con = sqlite3.connect(BOARD_DB, timeout=1)
        rows = con.execute("SELECT src, dst FROM aliases").fetchall()
        con.close()
    except sqlite3.Error:
        return {}
    return dict(rows)


def all_sessions() -> list[dict]:
    global TITLES, ALIASES
    TITLES = _story_titles()
    ALIASES = _aliases()
    now = time.time()
    sessions = (claude_sessions(now) if connections.STORE.get('claude')['enabled'] else []) + (codex_sessions(now) if connections.STORE.get('codex')['enabled'] else [])
    delegation.attach(sessions)
    order = {"입력 대기": 0, "작업 중": 1, "쉬는 중": 2, "종료됨": 3}
    sessions.sort(key=lambda s: (order.get(s["state"], 3), -s["last_ts"]))
    return sessions
