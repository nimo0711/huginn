from __future__ import annotations

import json
import re
import threading
import time
import connections
from datetime import datetime

from enrich import _run

TTL = 180
VIEW_FIELDS = "subtasks,customfield_10020,priority,duedate,aggregatetimeoriginalestimate,aggregatetimespent,updated,created"
TAG_RE = re.compile(r"^(\s*\[[^\]]*\])+\s*")
def project_filter():
    project=connections.STORE.get('jira')['project']
    return 'project = "'+project+'"'


def jira_url(key):
    return connections.STORE.get('jira')['site']+'/browse/'+key

_result: dict = {}
_lock = threading.Lock()
_busy = threading.Event()


def _jira_run(args):
    cfg=connections.STORE.get('jira')
    if not cfg['enabled'] or not connections.jira_account_matches(cfg['site']):return ''
    output=_run([connections.cli('acli'),*args[1:]])
    return output if connections.jira_account_matches(cfg['site']) else ''


def _view(key: str) -> dict:
    try:
        return json.loads(_jira_run([connections.cli('acli'), "jira", "workitem", "view", key, "--fields", VIEW_FIELDS, "--json"])).get("fields") or {}
    except ValueError:
        return {}


def _cat(f: dict) -> str:
    return (((f.get("status") or {}).get("statusCategory") or {}).get("key")) or ""


def _team() -> list:
    try:
        rows = json.loads(_jira_run([connections.cli('acli'), "jira", "workitem", "search", "--jql", project_filter()+" AND sprint in openSprints()", "--fields", "key,status,assignee,issuetype", "--paginate", "--json"]))
    except ValueError:
        return []
    people: dict = {}
    for r in rows if isinstance(rows, list) else []:
        f = r.get("fields") or {}
        name = (f.get("assignee") or {}).get("displayName") or "담당자 없음"
        p = people.setdefault(name, {"name": name, "new": 0, "indeterminate": 0, "done": 0, "subs": 0})
        p[_cat(f) or "new"] = p.get(_cat(f) or "new", 0) + 1
        if (f.get("issuetype") or {}).get("subtask") or (f.get("issuetype") or {}).get("name") == "하위작업":
            p["subs"] += 1
    return sorted(people.values(), key=lambda p: -(p["new"] + p["indeterminate"] + p["done"]))


def _carried(keys: list, start: str) -> int:
    if not keys or not start:
        return 0
    try:
        local = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
        jql = f'{project_filter()} AND parent in ({",".join(keys)}) AND resolved < "{local}"'
        rows = json.loads(_jira_run([connections.cli('acli'), "jira", "workitem", "search", "--jql", jql, "--fields", "key", "--paginate", "--json"]))
    except ValueError:
        return 0
    return len(rows) if isinstance(rows, list) else 0


def _refresh() -> None:
    try:
        signature=connections.STORE.fingerprint()
        cfg=connections.STORE.get('jira')
        if not cfg['enabled'] or not cfg['project'] or not connections.jira_account_matches(cfg['site']):return
        try:
            rows = json.loads(_jira_run([connections.cli('acli'), "jira", "workitem", "search", "--jql", project_filter()+" AND sprint in openSprints() AND assignee = currentUser() AND issuetype not in subTaskIssueTypes() ORDER BY key DESC", "--fields", "key,summary,status,issuetype", "--paginate", "--json"]))
        except ValueError:
            return
        items, sprint, meta = [], "", {}
        for r in rows if isinstance(rows, list) else []:
            f = r.get("fields") or {}
            st = f.get("status") or {}
            detail = _view(r.get("key") or "")
            subs = detail.get("subtasks") or []
            done = sum(1 for s in subs if (((s.get("fields") or {}).get("status") or {}).get("statusCategory") or {}).get("key") == "done")
            if not sprint:
                active = [s for s in detail.get("customfield_10020") or [] if isinstance(s, dict) and s.get("state") == "active"]
                if active:
                    sprint = active[0].get("name") or ""
                    meta = {k: active[0].get(k) or "" for k in ("startDate", "endDate", "goal")}
            items.append({
                "key": r.get("key"),
                "title": TAG_RE.sub("", f.get("summary") or ""),
                "tags": re.findall(r"\[([^\]]+)\]", (TAG_RE.match(f.get("summary") or "") or re.match("", "")).group(0)),
                "type": (f.get("issuetype") or {}).get("name") or "",
                "status": st.get("name") or "",
                "cat": (st.get("statusCategory") or {}).get("key") or "",
                "subs": len(subs),
                "subs_done": done,
                "subtasks": [{"url":jira_url(x.get("key") or ""),"key": x.get("key"), "title": TAG_RE.sub("", (x.get("fields") or {}).get("summary") or ""), "status": (((x.get("fields") or {}).get("status")) or {}).get("name") or "", "cat": _cat(x.get("fields") or {})} for x in subs],
                "priority": (detail.get("priority") or {}).get("name") or "",
                "due": detail.get("duedate") or "",
                "est": detail.get("aggregatetimeoriginalestimate") or 0,
                "spent": detail.get("aggregatetimespent") or 0,
                "updated": detail.get("updated") or "",
                "created": detail.get("created") or "",
                "url": jira_url(r.get("key") or ""),
            })
        with _lock:
            if signature!=connections.STORE.fingerprint():return
            _result.clear()
            _result.update({"configuration":signature,"at": time.time(), "sprint": sprint, "start": meta.get("startDate", ""), "end": meta.get("endDate", ""), "goal": meta.get("goal", ""), "items": items, "team": _team(),
                            "carried": _carried([i["key"] for i in items if i["subs"]], meta.get("startDate", ""))})
    finally:
        _busy.clear()


def summary() -> dict:
    cfg=connections.STORE.get('jira')
    if not cfg['enabled'] or not cfg['project']:return {'items':[],'disabled':True}
    with _lock:
        out = dict(_result) if _result.get('configuration')==connections.STORE.fingerprint() else {}
    if (not out or time.time() - out.get("at", 0) > TTL) and not _busy.is_set():
        _busy.set()
        threading.Thread(target=_refresh, daemon=True).start()
    return out
