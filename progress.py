"""Read explicit plan checklists; never estimate effort from tool counts."""
from pathlib import Path
import re


def checklist(text):
    rows = []
    fence = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            mark = stripped[:3]
            if fence is None:
                fence = mark
            elif mark == fence:
                fence = None
            continue
        if fence:
            continue
        match = re.match(r'^(\s*)[-*]\s+\[([ xX~-])\]\s+(.+)$', line)
        if match:
            indent, flag, title = match.groups()
            rows.append((len(indent.expandtabs(4)), {'title': title.strip()[:200],
                         'state': 'done' if flag.lower() == 'x' else 'active' if flag in '~-' else 'pending'}))
    if not rows or len(rows) > 200:
        return []
    depth = min(indent for indent, _ in rows)
    return [item for indent, item in rows if indent == depth]


def read_plan(path, root):
    try:
        file = Path(path).resolve()
        relative = file.relative_to(Path(root).resolve())
        if '계획서' not in relative.parts[:-1] or file.suffix.lower() != '.md':
            return None
        stat = file.stat()
        if stat.st_size > 256 * 1024:
            return None
        items = checklist(file.read_text(encoding='utf-8'))
        return {'items': items, 'rel': str(relative), 'ts': stat.st_mtime,
                'source': '계획서 체크리스트'} if items else None
    except (OSError, ValueError, UnicodeError):
        return None
