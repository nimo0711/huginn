import json
from pathlib import Path
import shlex
import sys

SETTINGS = Path.home() / ".claude" / "settings.json"
COMMAND = shlex.join([sys.executable, str(Path(__file__).resolve().with_name('claude_hook.py'))])
PLAN = {"SessionStart": None, "SessionEnd": None, "Stop": None, "UserPromptSubmit": None, "PostToolUse": "Bash"}

def install(settings=SETTINGS, command=COMMAND):
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})
    for event, matcher in PLAN.items():
        groups = hooks.setdefault(event, [])
        if any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
            continue
        group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {e: sum(h.get("command") == command for g in hooks[e] for h in g["hooks"]) for e in PLAN}


if __name__ == '__main__':
    print(install())
