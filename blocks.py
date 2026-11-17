from __future__ import annotations

import re

BLOCK_RE = re.compile(
    r"^(PreToolUse:\S+ hook error|Permission for this action was denied|The user doesn't want to proceed|User rejected|The user rejected)"
)


def result_text(body) -> str:
    if isinstance(body, list):
        return " ".join(x.get("text", "") for x in body if isinstance(x, dict))
    return str(body or "")


def is_blocked(body) -> bool:
    return bool(BLOCK_RE.match(result_text(body).lstrip()))


def block_kind(body) -> str:
    text = result_text(body).lstrip()
    if not BLOCK_RE.match(text):
        return ""
    if "auto mode" in text:
        return "auto"
    if text.startswith("PreToolUse:"):
        return "hook"
    return "user"
