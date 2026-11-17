from __future__ import annotations

import json
import math
import mimetypes
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import scan
import sprint
import usage
import local_runtime
import board_actions
import branding

HOME = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WORK_BOARD_DB", HOME / "board.db"))
PORT = int(os.environ.get("WORK_BOARD_PORT", "8790"))
ACTION_TOKEN = secrets.token_urlsafe(32)
ACTIONS = board_actions.ActionStore(HOME / ".actions")
SESSION_IDLE_SEC = 30 * 60
SPOOL = os.environ.get("WORK_BOARD_SPOOL", "/tmp/work-board-spool")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    story TEXT NOT NULL DEFAULT '',
    item TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL DEFAULT '',
    before_value TEXT NOT NULL DEFAULT '',
    after_value TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_events_story ON events (story, ts);
CREATE INDEX IF NOT EXISTS ix_events_session ON events (session, ts);
CREATE TABLE IF NOT EXISTS aliases (src TEXT PRIMARY KEY, dst TEXT NOT NULL);
"""

FIELDS = (
    "agent", "session", "repo", "branch", "story", "item", "kind",
    "status", "title", "message", "metric", "before_value", "after_value",
)
KINDS = {"log", "status", "metric", "session_start", "session_end", "story", "heartbeat"}

_lock = threading.Lock()
_scan_lock = threading.Lock()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _event_row(payload: dict) -> tuple[dict, float]:
    if not isinstance(payload, dict):
        raise ValueError("기록은 JSON 객체로 입력해 주세요.")
    kind = str(payload.get("kind") or "log")
    if kind not in KINDS:
        raise ValueError(f"알 수 없는 종류: {kind}")
    row = {name: str(payload.get(name) or "")[:4000] for name in FIELDS}
    row["kind"] = kind
    raw_ts = payload.get("ts")
    if raw_ts is not None and (isinstance(raw_ts, bool) or not isinstance(raw_ts, (int, float, str))):
        raise ValueError("기록 시간이 올바르지 않습니다.")
    try:
        ts = float(raw_ts or time.time())
    except (ValueError, OverflowError):
        raise ValueError("기록 시간이 올바르지 않습니다.") from None
    if not math.isfinite(ts) or ts < 0:
        raise ValueError("기록 시간이 올바르지 않습니다.")
    return row, ts


def add_event(payload: dict) -> int:
    row, ts = _event_row(payload)
    with _lock, connect() as conn:
        cur = conn.execute(
            f"INSERT INTO events (ts, {', '.join(FIELDS)}) VALUES (?, {', '.join('?' * len(FIELDS))})",
            [ts, *(row[name] for name in FIELDS)],
        )
        return int(cur.lastrowid or 0)


def stories(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT story, MAX(ts) AS last_ts, COUNT(*) AS events
        FROM events WHERE story <> '' GROUP BY story ORDER BY last_ts DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        head = conn.execute(
            "SELECT title, status FROM events WHERE story = ? AND kind = 'story' ORDER BY ts DESC LIMIT 1",
            (r["story"],),
        ).fetchone()
        out.append({
            "story": r["story"],
            "title": head["title"] if head else "",
            "status": head["status"] if head else "",
            "last_ts": r["last_ts"],
            "events": r["events"],
        })
    return out


def items(conn: sqlite3.Connection, story: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT item, ts, status, title, message, agent FROM events
        WHERE story = ? AND item <> '' AND kind = 'status' ORDER BY ts
        """,
        (story,),
    ).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        cur = latest.setdefault(r["item"], {"item": r["item"], "title": "", "status": "", "message": "", "ts": 0, "agent": ""})
        if r["title"]:
            cur["title"] = r["title"]
        if r["status"]:
            cur["status"] = r["status"]
        if r["message"]:
            cur["message"] = r["message"]
        cur["ts"] = r["ts"]
        cur["agent"] = r["agent"]
    return list(latest.values())


def metrics(conn: sqlite3.Connection, story: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT item, metric, before_value, after_value, message, ts FROM events
        WHERE story = ? AND kind = 'metric' ORDER BY ts
        """,
        (story,),
    ).fetchall()
    latest: dict[tuple, dict] = {}
    for r in rows:
        latest[(r["item"], r["metric"])] = dict(r)
    return list(latest.values())


def timeline(conn: sqlite3.Connection, story: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ts, agent, session, repo, branch, item, kind, status, title, message, metric, before_value, after_value
        FROM events WHERE story = ? AND kind IN ('log', 'status', 'metric')
        ORDER BY ts DESC, id DESC LIMIT ?
        """,
        (story, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT session, MAX(ts) AS last_ts, MIN(ts) AS first_ts FROM events
        WHERE session <> '' GROUP BY session ORDER BY last_ts DESC LIMIT 200
        """
    ).fetchall()
    now = time.time()
    out = []
    for r in rows:
        last = conn.execute(
            "SELECT agent, repo, branch, story, kind, message, title FROM events WHERE session = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (r["session"],),
        ).fetchone()
        ended = last["kind"] == "session_end"
        active = not ended and now - r["last_ts"] < SESSION_IDLE_SEC
        if not active and now - r["last_ts"] > 6 * 3600:
            continue
        story = last["story"] or (conn.execute(
            "SELECT story FROM events WHERE session = ? AND story <> '' ORDER BY ts DESC LIMIT 1", (r["session"],)
        ).fetchone() or {"story": ""})["story"]
        doing = conn.execute(
            "SELECT message, title FROM events WHERE session = ? AND kind IN ('log','status','metric') ORDER BY ts DESC, id DESC LIMIT 1",
            (r["session"],),
        ).fetchone()
        out.append({
            "session": r["session"],
            "agent": last["agent"],
            "repo": last["repo"],
            "branch": last["branch"],
            "story": story,
            "last_ts": r["last_ts"],
            "first_ts": r["first_ts"],
            "active": active,
            "doing": (doing["message"] or doing["title"]) if doing else "",
        })
    return out


def state(story: str | None, limit: int) -> dict:
    with connect() as conn:
        all_stories = stories(conn)
        chosen = story or (all_stories[0]["story"] if all_stories else "")
        return {
            "now": time.time(),
            "stories": all_stories,
            "story": chosen,
            "items": items(conn, chosen) if chosen else [],
            "metrics": metrics(conn, chosen) if chosen else [],
            "timeline": timeline(conn, chosen, limit) if chosen else [],
            "sessions": sessions(conn),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "work-board"

    def log_message(self, format, *args) -> None:
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            path = urlparse(self.path).path
        except ValueError:
            path = ""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if path=='/':
            self.send_header('Content-Security-Policy',"frame-ancestors 'none'")
            self.send_header('X-Frame-Options','DENY')
        if path.startswith("/doc/"):
            # Render interactive documents in an opaque origin: a document must
            # not read the board's action token or launch local applications.
            self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-forms allow-popups")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _local_request(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        allowed_hosts = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        allowed_origins = {"http://" + host for host in allowed_hosts}
        if (len(hosts) != 1 or hosts[0] not in allowed_hosts
                or len(origins) > 1 or origins and origins[0] not in allowed_origins):
            self._json(403, {"error": "작업 보드 주소에서 열어 주세요."})
            return False
        return True

    def _read_json_body(self, maximum: int):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            raise ValueError("요청 본문 길이를 확인해 주세요.")
        text = lengths[0]
        if not text.isascii() or not text.isdecimal() or len(text) > 10:
            raise ValueError("요청 본문 길이를 확인해 주세요.")
        length = int(text)
        if not 0 < length <= maximum:
            raise ValueError("요청 크기가 올바르지 않습니다.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("요청 본문이 완전하지 않습니다.")
        try:
            return json.loads(raw)
        except RecursionError:
            raise ValueError("요청 자료의 중첩이 너무 깊습니다.") from None

    def do_GET(self) -> None:
        if not self._local_request():
            return
        try:
            url = urlparse(self.path)
        except ValueError:
            self._json(400, {"error": "요청 주소가 올바르지 않습니다."})
            return
        if url.path == "/":
            self._send(200, (HOME / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/assets/brand.svg":
            self._send(200, branding.icon_svg().encode(), "image/svg+xml; charset=utf-8")
        elif url.path == "/api/state":
            try:
                q = parse_qs(url.query, keep_blank_values=True, max_num_fields=100)
                story = (q.get("story") or [None])[0]
                limits = q.get("limit", ["300"])
                raw_limit = limits[0]
                if (len(limits) != 1 or not raw_limit.isascii() or not raw_limit.isdecimal()
                        or len(raw_limit) > 10 or int(raw_limit) < 1):
                    raise ValueError("조회 개수는 양의 정수로 입력해 주세요.")
                limit = int(raw_limit)
            except ValueError:
                self._json(400, {"error": "조회 조건을 확인해 주세요. 조회 개수는 양의 정수여야 합니다."})
                return
            self._json(200, state(story, min(limit, 2000)))
        elif url.path == "/stories":
            self._send(200, (HOME / "stories.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/sessions":
            with _scan_lock:
                found = scan.all_sessions()
            local_runtime.attach(found)
            self._json(200, {"now": time.time(), "sessions": found, "sprint": sprint.summary(), "usage": usage.summary()})
        elif url.path == "/api/actions":
            if self.headers.get('Host') not in {f'127.0.0.1:{PORT}',f'localhost:{PORT}'}:
                self._json(403,{'error':'작업 보드 주소에서 열어 주세요.'});return
            sid = (parse_qs(url.query).get("sid") or [""])[0]
            with _scan_lock:
                session = next((s for s in scan.all_sessions() if s["id"] == sid), None)
            if sid and (not session or not session.get("cwd_path")):
                self._json(400, {"error": "선택한 세션의 작업 폴더를 찾을 수 없습니다."})
            else:
                cwd=session['cwd_path'] if session else str(ACTIONS.home)
                self._json(200, {"token": ACTION_TOKEN, "skills": ACTIONS.catalog(cwd),
                    "settings": ACTIONS.settings(), "shortcuts": ACTIONS.shortcuts(cwd), "guide": ACTIONS.guide(cwd),
                    "connections": ACTIONS.connections.catalog(),
                    "connection": ACTIONS.delivery.describe(session),
                    "delivery": ACTIONS.delivery.lookup(sid,(parse_qs(url.query).get("request_id") or [""])[0])})
        elif url.path.startswith("/doc/"):
            self._doc(unquote(url.path[len("/doc/"):]))
        elif url.path == "/api/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "없는 경로"})

    def _doc(self, rel: str) -> None:
        if "\x00" in rel:
            self._json(400, {"error": "문서 경로가 올바르지 않습니다."})
            return
        root = os.path.realpath(os.path.expanduser("~/vtm/문서"))
        real = os.path.realpath(os.path.join(root, rel))
        if not real.startswith(root + os.sep) or not os.path.isfile(real):
            self._json(404, {"error": "없는 문서"})
            return
        ctype = mimetypes.guess_type(real)[0] or "application/octet-stream"
        if ctype.startswith("text/") or real.endswith((".md", ".json", ".txt")):
            ctype = ("text/plain" if real.endswith(".md") else ctype) + "; charset=utf-8"
        with open(real, "rb") as f:
            self._send(200, f.read(), ctype)

    def _alias(self) -> None:
        origin = self.headers.get("Origin") or ""
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            self._json(403, {"error": "다른 출처 요청은 받지 않음"})
            return
        try:
            body = self._read_json_body(65536)
            if not isinstance(body, dict):
                raise ValueError("별칭은 JSON 객체로 입력해 주세요.")
            src, dst = body.get("src", ""), body.get("dst", "")
            if (not isinstance(src, str) or not isinstance(dst, str)
                    or any(len(value) > 4000 or "\x00" in value for value in (src, dst))):
                raise ValueError("별칭은 텍스트로 입력해 주세요.")
            src, dst = src.strip(), dst.strip()
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "형식 오류"})
            return
        if not src:
            self._json(400, {"error": "src 필요"})
            return
        with _lock, connect() as conn:
            if dst:
                conn.execute("INSERT INTO aliases (src, dst) VALUES (?, ?) ON CONFLICT(src) DO UPDATE SET dst = excluded.dst", (src, dst))
            else:
                conn.execute("DELETE FROM aliases WHERE src = ?", (src,))
        self._json(200, {"ok": True})

    def do_POST(self) -> None:
        if not self._local_request():
            return
        try:
            path = urlparse(self.path).path
        except ValueError:
            self._json(400, {"error": "요청 주소가 올바르지 않습니다."})
            return
        if path == "/api/actions":
            self._action()
            return
        if path == "/api/alias":
            self._alias()
            return
        if path != "/api/events":
            self._json(404, {"error": "없는 경로"})
            return
        if self.headers.get("Origin"):
            self._json(403, {"error": "브라우저 요청은 받지 않음"})
            return
        try:
            payload = self._read_json_body(1024 * 1024)
            events = payload if isinstance(payload, list) else [payload]
            # Reject an invalid batch before committing any earlier valid row.
            for event in events:
                _event_row(event)
            ids = [add_event(e) for e in events]
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"ids": ids})

    def _action(self) -> None:
        allowed = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
        if (self.headers.get("Origin") not in allowed
                or self.headers.get("Host") not in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
                or not secrets.compare_digest(self.headers.get("X-Work-Board-Token", ""), ACTION_TOKEN)
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
            self._json(403, {"error": "작업 보드에서 직접 실행해 주세요."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 65536: raise ValueError("실행 요청 크기가 올바르지 않습니다.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict): raise ValueError("요청 형식이 올바르지 않습니다.")
            with _scan_lock:
                session = next((s for s in scan.all_sessions() if s["id"] == body.get("sid")), None)
            action = body.get("action")
            if (action in ('launch','deliver','focus') or body.get('sid')) and (not session or not session.get('cwd_path')):
                raise ValueError("세션을 찾을 수 없습니다. 새로 고침해 주세요.")
            cwd=session['cwd_path'] if session else str(ACTIONS.home)
            if action == 'connection_save':
                result={'ok':True,'values':ACTIONS.connections.save(body.get('kind'),body.get('values'),expected=body.get('expected')),'message':'설정을 저장했습니다. 로그인 후 연결 확인을 눌러 주세요.'}
            elif action == 'connection_remove':
                ACTIONS.connections.remove(body.get('kind'));result={'ok':True,'message':'보드 연결을 제거했습니다.'}
            elif action == 'connection_check':
                result=ACTIONS.connections.check(body.get('kind'))
            elif action == 'connection_login':
                result=ACTIONS.login(body.get('kind'),body.get('request_id'),body.get('switch',False))
            elif action == 'configure':
                result={'ok':True,'settings':ACTIONS.configure(body.get('settings')),'message':'연결 설정을 저장했습니다.'}
            elif action == "favorite":
                ACTIONS.favorite(body.get("skill"), body.get("enabled") is True, cwd)
                result = {"ok": True}
            elif action == "deliver":
                result = ACTIONS.deliver(body.get("skill"), session, body.get("request_id"), body.get("prompt", ""))
            elif action == "launch":
                launched = ACTIONS.launch(body.get("skill"), session["cwd_path"], body.get("request_id"), body.get("prompt", ""))
                result = {k: v for k,v in launched.items() if k != "launcher"}
            elif action == "focus":
                procs = local_runtime.processes()
                terminal = local_runtime.session_terminal(session, procs)
                if not terminal: raise ValueError("실행 중인 터미널 앱을 찾을 수 없습니다.")
                import subprocess
                opened = subprocess.run(["/usr/bin/open", "-a", terminal["path"]], capture_output=True, timeout=10)
                if opened.returncode: raise RuntimeError("터미널 앱을 열지 못했습니다.")
                result = {"ok": True, "message": terminal["app"] + "를 열었습니다. 해당 탭을 선택해 주세요."}
            else: raise ValueError("지원하지 않는 동작입니다.")
            self._json(200, result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            message = "세션 연결이 끊겼거나 응답하지 않습니다. 해당 세션을 확인해 주세요." if body.get("action") == "deliver" else "실행하지 못했습니다. 터미널 앱과 CLI 설치 상태를 확인해 주세요."
            self._json(409, {"error": message})


def drain_spool() -> None:
    while True:
        try:
            names = sorted(n for n in os.listdir(SPOOL) if n.endswith(".jsonl"))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(SPOOL, name)
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            try:
                                add_event(json.loads(line))
                            except ValueError:
                                pass
                os.remove(path)
            except OSError:
                pass
        time.sleep(3)


def main() -> None:
    init_db()
    threading.Thread(target=drain_spool, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
