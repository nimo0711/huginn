# Forkrail

병렬로 진행되는 Claude·Codex 작업의 맥락과 진척을 따라가는 개인용 macOS 앱입니다. **Fork + Rail**이라는 이름처럼, 갈라진 작업의 진행 경로와 응답이 필요한 지점을 한 화면에서 확인합니다.

앱은 **Swift/AppKit + WKWebView**로 만든 macOS 창과 **Python 로컬 서비스**로 구성됩니다. 브라우저에서도 같은 화면을 열 수 있습니다. 앱 창을 닫아도 별도로 실행한 로컬 서비스는 계속 동작합니다.

## 주요 기능

- Claude·Codex 세션과 하위 평가 세션의 관계 표시
- Jira 스토리·태스크 또는 Jira 없는 작업 묶음 기준의 진척도
- 요청·결과·파일 변경·진행 기록 확인
- 로컬 서버 탐지와 터미널 앱 열기
- 설치된 스킬 검색, 요청 내용 입력, 연결된 세션 또는 새 터미널에서 실행
- Claude·Codex 사용량, 도구 실행 결과, 집계 범위 확인
- Jira·GitLab·로컬 Git 연결과 AI용 설정 도움말

연결은 설정 화면에서 선택합니다. Jira·GitLab 인증은 각각 `acli`와 `glab`에 로그인된 계정을 사용합니다. 스킬 목록은 현재 Mac에 설치·활성화된 항목에서 읽습니다.

## 실행 환경

- macOS 13 이상
- Python 3.9 이상. 서버는 Python 표준 라이브러리만 사용합니다.
- 앱 빌드: Xcode Command Line Tools의 `swiftc`, `iconutil`, `codesign`
- 프런트엔드 테스트: Node.js 18 이상
- 선택 연동: Claude CLI, Codex, Git, Atlassian CLI(`acli`), GitLab CLI(`glab`)

## 실행

```sh
git clone https://github.com/nimo0711/work-board.git
cd work-board
python3 server.py
```

서비스가 실행되면 <http://127.0.0.1:8790>에서 보드를 엽니다. 서버는 루프백 주소에만 바인딩합니다. 이미 이 포트에서 작업 보드를 실행하고 있다면 기존 서비스를 그대로 사용하세요.

다른 터미널에서 macOS 앱을 빌드합니다.

```sh
bash scripts/build_app.sh
open 'build/Forkrail.app'
```

빌드 결과는 현재 Mac 아키텍처용 앱입니다. 생성한 앱은 로컬 실행용 서명을 사용하며, Apple 공증을 받은 배포본은 아닙니다. 앱을 `~/Applications`로 옮겨 사용할 수 있습니다. 이 앱은 기본 포트 `8790`의 서비스를 열며 Python 서비스를 자체 실행하지 않습니다.

## 로그인할 때 서비스 자동 실행

처음 설치하는 Mac에서 다음 명령으로 실행 경로가 반영된 LaunchAgent 파일을 만듭니다. 이미 다른 작업 보드 LaunchAgent가 있다면 중복 설치하지 마세요.

```sh
python3 scripts/service_plist.py
plutil -lint build/local.work-board.plist
mkdir -p "$HOME/Library/LaunchAgents"
cp build/local.work-board.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.work-board.plist"
```

직접 실행 중인 `python3 server.py`는 먼저 Ctrl+C로 종료합니다. 저장소나 Python 경로를 옮겼다면 파일을 다시 생성해야 합니다.

자동 실행 해제:

```sh
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.work-board.plist"
```

## Claude 작업 기록 연결

자동 세션 조회 외에 커밋·명령 등의 보드 기록을 남기려면 다음 명령을 한 번 실행합니다. 기존 Claude 설정을 읽고 보드 훅만 추가합니다.

```sh
python3 install_claude_hooks.py
```

직접 기록하는 방법은 [AGENT_GUIDE.md](AGENT_GUIDE.md)에 있습니다.

## 데이터와 연결

보드는 현재 사용자의 `~/.claude`, `~/.codex` 기록을 읽습니다. Jira·GitLab·로컬 Git의 대상은 설정에서 등록합니다. 제목 요약을 켜면 Claude CLI를 호출하므로 사용량이 발생할 수 있습니다.

다음 데이터는 각 Mac에 생성되며 Git에 포함하지 않습니다.

| 경로 | 내용 |
|---|---|
| `board.db*` | 작업 기록 DB |
| `.actions/` | 연결 설정, 바로가기, 실행 요청 상태 |
| `work_titles.json`, `jira_titles.json`, `story_cache.json` | 로컬 요약·조회 캐시 |
| `*.log` | 실행 로그 |
| `/tmp/work-board-spool` | 보드가 받지 못한 기록의 임시 저장소 |

환경 변수:

| 이름 | 기본값 | 용도 |
|---|---|---|
| `WORK_BOARD_PORT` | `8790` | 웹 서비스 포트. 기본 macOS 앱은 8790을 사용합니다. |
| `WORK_BOARD_DB` | 저장소의 `board.db` | 작업 DB 경로 |
| `WORK_BOARD_URL` | `http://127.0.0.1:8790` | `wb` 명령의 전송 대상 |
| `WORK_BOARD_SPOOL` | `/tmp/work-board-spool` | 미전송 기록 저장 경로 |

## 검증

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.cjs
bash scripts/build_app.sh
```

연결 설정은 계정 인증 완료와 구분합니다. 로컬 사용량은 확인 가능한 기록의 합계이며 서비스의 청구서나 계정 한도와 다를 수 있습니다. 세션 감지는 Claude·Codex의 로컬 기록 형식에 의존합니다.

## 소스 구조

- `app/`: macOS 창, 알림, 앱 아이콘
- `server.py`: 로컬 HTTP 서비스와 작업 기록
- `index.html`, `stories.html`: 보드와 기록 화면
- `scan.py`, `usage.py`: 세션·사용량 읽기
- `connections.py`, `board_actions.py`: 연결·스킬·바로가기
- `wb`, `wbcore.py`, `claude_hook.py`: CLI와 Claude 훅
- `scripts/`: 앱 빌드와 자동 실행 설정 생성
- `tests/`: 서버·세션·진척도·설정·화면 로직 테스트
