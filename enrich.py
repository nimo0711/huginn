from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote
import connections
from runtime_paths import DATA_DIR
from platform_support import replace_file, run_hidden

CACHE = os.path.join(DATA_DIR, "story_cache.json")
TTL = 180
TAG_RE = re.compile(r"^(\s*\[[^\]]*\])+\s*")

_data: dict = {}
_busy: set = set()
_lock = threading.Lock()
_configuration = None


def _load() -> None:
    try:
        with open(CACHE, encoding="utf-8") as f:
            _data.update(json.load(f))
    except (OSError, ValueError):
        pass


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(_data, f, ensure_ascii=False)
        replace_file(CACHE + ".tmp", CACHE)
    except OSError:
        pass


def _run(args: list, cwd: str | None = None) -> str:
    try:
        return run_hidden(args, capture_output=True, encoding="utf-8", errors="replace", cwd=cwd, timeout=25).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _cat(status: dict | None) -> str:
    return ((status or {}).get("statusCategory") or {}).get("key") or ""


def _jira(key: str) -> dict:
    cfg=connections.STORE.get('jira')
    if cfg['site'] and not connections.jira_account_matches(cfg['site']):return {}
    if cfg['project'] and not key.startswith(cfg['project']+'-'):return {}
    out = _run([connections.cli('acli'), "jira", "workitem", "view", key, "--fields", "summary,status,subtasks,resolutiondate,issuetype,parent", "--json"])
    if cfg['site'] and not connections.jira_account_matches(cfg['site']):return {}
    try:
        f = json.loads(out).get("fields") or {}
    except ValueError:
        return {}
    if not f.get("summary"):
        return {}
    parent = f.get("parent") or {}
    pf = parent.get("fields") or {}
    subs = []
    for s in f.get("subtasks") or []:
        sf = s.get("fields") or {}
        subs.append({"key": s.get("key"), "title": TAG_RE.sub("", sf.get("summary") or ""), "status": (sf.get("status") or {}).get("name") or "", "cat": _cat(sf.get("status"))})
    return {
        "title": TAG_RE.sub("", f.get("summary") or ""),
        "status": (f.get("status") or {}).get("name") or "",
        "cat": _cat(f.get("status")),
        "resolved_at": f.get("resolutiondate") or "",
        "subtasks": subs,
        "issue_type": f.get("issuetype") or {},
        "parent": {"key": parent["key"], "title": TAG_RE.sub("", pf.get("summary") or ""),
                   "issue_type": pf.get("issuetype") or {}} if parent.get("key") else None,
        "hierarchy_version": 1,
    }


def _mrs(key: str) -> list:
    cfg=connections.STORE.get('gitlab')
    if not cfg['enabled'] or not cfg['host'] or not cfg['group']:return []
    _,out = connections.gitlab_api(f"groups/{quote(cfg['group'],safe='')}/merge_requests?search={quote(key,safe='')}&state=all&per_page=50",cfg)
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    mrs = []
    for m in rows if isinstance(rows, list) else []:
        if key not in (m.get("title") or "") and key not in (m.get("source_branch") or ""):
            continue
        ref = (m.get("references") or {}).get("full") or ""
        mrs.append({
            "repo": ref.split("/")[-1].split("!")[0].replace("-withvtm", ""),
            "iid": m.get("iid"),
            "state": m.get("state") or "",
            "title": TAG_RE.sub("", m.get("title") or ""),
            "url": m.get("web_url") or "",
            "branch": m.get("source_branch") or "",
            "target": m.get("target_branch") or "",
            "merged_at": m.get("merged_at") or "",
        })
    return mrs


def _commits(key: str) -> list:
    dirs = set()
    cfg=connections.STORE.get('git')
    if not cfg['enabled']:return []
    for root in cfg['roots']:
        for pattern in ('','*','*/*'):
            for d in ([Path(root)] if not pattern else Path(root).glob(pattern)):
                if (d/'.git').exists():dirs.add(str(d))
    seen, out = set(), []
    for d in sorted(dirs):
        repo = re.sub(r"-.*", "", os.path.basename(d))
        log = _run([connections.cli("git") if connections.IS_WINDOWS else "git", "-C", d, "log", "--all", f"--grep={key}", "--format=%ct|%s", "-n", "40"])
        for line in log.splitlines():
            ts, _, subj = line.partition("|")
            body = TAG_RE.sub("", subj).strip()
            head, _, why = body.partition(" — ")
            if not head or head in seen or head.startswith(("Merge branch", "Merge remote", "Revert \"Merge")):
                continue
            seen.add(head)
            out.append({"ts": int(ts or 0), "repo": repo, "title": head, "why": why})
    out.sort(key=lambda c: -c["ts"])
    return out[:40]


def _refresh(key: str) -> None:
    try:
        signature=connections.STORE.fingerprint()
        info = {"key": key, "at": time.time(), "resolved_at": "", "hierarchy_checked_at": time.time()}
        if connections.STORE.get('jira')['enabled']:info.update(_jira(key))
        info["mrs"] = _mrs(key)
        info["commits"] = _commits(key)
        with _lock:
            if signature!=connections.STORE.fingerprint():return
            old = _data.get(key) or {}
            if not info.get("title") and old.get("title"):
                for k in ("title", "status", "cat", "subtasks", "resolved_at", "issue_type", "parent", "hierarchy_version"):
                    info[k] = old.get(k)
            _data[key] = info
            _save()
    finally:
        _busy.discard(key)


def story(key: str) -> dict:
    global _configuration
    signature=connections.STORE.fingerprint()
    with _lock:
        if _configuration!=signature:_data.clear();_configuration=signature
    if not any(connections.STORE.get(kind)['enabled'] for kind in ('jira','gitlab','git')):return {}
    info = _data.get(key)
    if (not info or "resolved_at" not in info or (info.get("hierarchy_version") != 1 and not info.get("hierarchy_checked_at")) or time.time() - info.get("at", 0) > TTL) and key not in _busy:
        _busy.add(key)
        threading.Thread(target=_refresh, args=(key,), daemon=True).start()
    return info or {}


def work_unit(key: str, info: dict) -> dict:
    """Resolve a Story ancestor, otherwise the independent issue owning the work.

    Jira's Story and Task share hierarchy level zero. Use actual type metadata,
    never project keys, issue titles, or an Epic ancestor as a Story substitute.
    Embedded parent fields allow resolution while richer cache data is loading.
    """
    def kind(row):
        typ = row.get("issue_type") or {}
        if (typ.get("name") or "").casefold() in ("story", "스토리"):
            return "story"
        return "task" if typ else "issue"

    current, row = key, info
    fallback = {"key": key, "kind": kind(info), "title": info.get("title") or ""}
    seen = set()
    while current and current not in seen and len(seen) < 10:
        seen.add(current)
        if kind(row) == "story":
            return {"key": current, "kind": "story", "title": row.get("title") or ""}
        parent = row.get("parent") or {}
        if not parent.get("key") or parent["key"] in seen:
            break
        parent_info = dict(parent)
        parent_info.update({k: v for k, v in story(parent["key"]).items()
                            if k not in ("title", "issue_type") or v})
        typ = parent_info.get("issue_type") or {}
        if typ.get("hierarchyLevel", 0) > 0 or (typ.get("name") or "").casefold() in ("epic", "에픽"):
            break
        # A subtask without a Story belongs to its ordinary parent task.
        if (row.get("issue_type") or {}).get("subtask") and typ and not typ.get("subtask"):
            fallback = {"key": parent["key"], "kind": kind(parent_info), "title": parent_info.get("title") or ""}
        current, row = parent["key"], parent_info
    return fallback


def stage(info: dict) -> str:
    if not info:
        return ""
    if info.get("cat") == "done":
        return "완료"
    mrs = [m for m in info.get("mrs") or [] if m["state"] != "closed"]
    if mrs and all(m["state"] == "merged" for m in mrs):
        return "머지됨"
    if any(m["state"] == "opened" for m in mrs):
        return "리뷰 대기"
    return ""


_load()
