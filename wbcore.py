from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

URL = os.environ.get("WORK_BOARD_URL", "http://127.0.0.1:8790")
SPOOL = os.environ.get("WORK_BOARD_SPOOL", "/tmp/work-board-spool")
STORY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
AGENTS = ("claude", "codex")


def _run(args: list[str], cwd: str | None = None) -> str:
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def agent_process() -> tuple[str, int]:
    pid = os.getppid()
    for _ in range(20):
        line = _run(["ps", "-o", "ppid=,comm=", "-p", str(pid)])
        if not line:
            break
        ppid, _, comm = line.strip().partition(" ")
        name = os.path.basename(comm.strip()).lower()
        for agent in AGENTS:
            if name == agent or name.startswith(agent + "-") or name.startswith(agent + "."):
                return agent, pid
        if not ppid.strip().isdigit() or int(ppid) <= 1:
            break
        pid = int(ppid)
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_HOME"):
        return "codex", os.getppid()
    if os.environ.get("CLAUDECODE"):
        return "claude", os.getppid()
    return "사람", os.getppid()


def git_context(cwd: str) -> tuple[str, str]:
    top = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    common = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    repo = os.path.basename(os.path.dirname(common)) if common.endswith("/.git") else os.path.basename(top)
    branch = _run(["git", "branch", "--show-current"], cwd)
    return repo, branch


def context(cwd: str | None = None) -> dict:
    cwd = cwd or os.getcwd()
    agent, pid = agent_process()
    repo, branch = git_context(cwd)
    match = STORY_RE.search(branch or "")
    return {
        "agent": agent,
        "session": f"{agent}-{pid}",
        "repo": repo,
        "branch": branch,
        "story": match.group(1) if match else "",
    }


def spool(events: list[dict]) -> bool:
    try:
        os.makedirs(SPOOL, exist_ok=True)
        path = os.path.join(SPOOL, f"{time.time():.6f}-{os.getpid()}.jsonl")
        with open(path + ".part", "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(path + ".part", path)
        return True
    except OSError:
        return False


def post(events: list[dict], quiet: bool = False) -> bool:
    now = time.time()
    events = [dict(e, ts=e.get("ts") or now) for e in events]
    data = json.dumps(events, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{URL}/api/events", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return True
    except OSError:
        pass
    if spool(events):
        return True
    if not quiet:
        print(f"작업 보드 기록 실패({URL}, {SPOOL})", file=sys.stderr)
    return False


def main() -> int:
    if len(sys.argv)>1 and sys.argv[1]=='actions':
        from actions_cli import main as actions_main
        return actions_main(sys.argv[2:])
    parser = argparse.ArgumentParser(prog="wb", description="작업 보드에 한 줄 기록")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--story", "-s", help="Jira 스토리 키(생략하면 브랜치 이름에서)")
    common.add_argument("-m", "--message", default="")

    p = sub.add_parser("story", parents=[common], help="스토리 제목·상태")
    p.add_argument("title")
    p.add_argument("--status", default="진행 중")

    p = sub.add_parser("status", parents=[common], help="항목(하위작업) 상태")
    p.add_argument("item")
    p.add_argument("state")
    p.add_argument("--title", default="")

    p = sub.add_parser("log", parents=[common], help="작업 기록 한 줄")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--item", default="")

    p = sub.add_parser("metric", parents=[common], help="전후 수치")
    p.add_argument("item")
    p.add_argument("name")
    p.add_argument("before")
    p.add_argument("after")

    args = parser.parse_args()
    base = context()
    if args.story:
        base["story"] = args.story
    if not base["story"]:
        print("스토리 키를 알 수 없음: 브랜치 이름에 키가 없으면 --story MP-1234 로 지정", file=sys.stderr)
        return 2

    event = dict(base)
    if args.cmd == "story":
        event.update(kind="story", title=args.title, status=args.status, message=args.message)
    elif args.cmd == "status":
        event.update(kind="status", item=args.item, status=args.state, title=args.title, message=args.message)
    elif args.cmd == "log":
        event.update(kind="log", item=args.item, message=args.text or args.message)
    else:
        event.update(kind="metric", item=args.item, metric=args.name, before_value=args.before,
                     after_value=args.after, message=args.message)
    return 0 if post([event]) else 1
