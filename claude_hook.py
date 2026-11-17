#!/usr/bin/python3
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from wbcore import _run, context, post

CD_RE = re.compile(r"^\s*cd\s+([^;&|]+?)\s*(?:&&|;)")


def command_dir(command: str, cwd: str) -> str:
    match = CD_RE.match(command)
    if not match:
        return cwd
    try:
        target = os.path.expanduser(shlex.split(match.group(1))[0])
    except ValueError:
        return cwd
    path = target if os.path.isabs(target) else os.path.join(cwd, target)
    return path if os.path.isdir(path) else cwd


def bash_events(data: dict, base: dict, cwd: str) -> list[dict]:
    command = (data.get("tool_input") or {}).get("command") or ""
    response = data.get("tool_response") or {}
    out = (response.get("stdout") or "") if isinstance(response, dict) else str(response)
    events = []
    if re.search(r"\bgit\b[^|;&]*\bcommit\b", command):
        line = _run(["git", "log", "-1", "--format=%ct%x09%h%x09%s"], cwd)
        stamp, _, rest = line.partition("\t")
        if stamp.isdigit() and time.time() - int(stamp) < 120:
            sha, _, subject = rest.partition("\t")
            events.append(dict(base, kind="log", message=f"커밋 {sha} {subject}"))
    if re.search(r"\bgit\b[^|;&]*\bpush\b", command) and response and not (isinstance(response, dict) and response.get("interrupted")):
        events.append(dict(base, kind="log", message=f"푸시 {base['branch']}"))
    mr = re.search(r"https?://\S+/merge_requests/\d+", out)
    if mr and re.search(r"\bglab\s+mr\s+(create|merge)\b", command):
        verb = "MR 생성" if " create" in command else "MR 머지"
        events.append(dict(base, kind="log", message=f"{verb} {mr.group(0)}"))
    return events


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    event_name = data.get("hook_event_name") or ""
    cwd = data.get("cwd") or os.getcwd()
    if event_name == "PostToolUse" and data.get("tool_name") == "Bash":
        cwd = command_dir((data.get("tool_input") or {}).get("command") or "", cwd)
    base = context(cwd)
    if event_name == "SessionStart":
        events = [dict(base, kind="session_start", message=f"세션 시작 {cwd}")]
    elif event_name == "SessionEnd":
        events = [dict(base, kind="session_end", message="세션 종료")]
    elif event_name in ("Stop", "UserPromptSubmit"):
        events = [dict(base, kind="heartbeat")]
    elif event_name == "PostToolUse" and data.get("tool_name") == "Bash":
        events = bash_events(data, base, cwd)
    else:
        events = []
    if events:
        post(events, quiet=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
