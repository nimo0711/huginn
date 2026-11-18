# Forkrail

Claude·Codex의 병렬 작업에서 세션의 관계, 현재 작업, 진척과 산출물을 한곳에서 확인하는 데스크톱 앱입니다.

## 설치

배포 대상은 Windows 10/11 x64, macOS 13 이상 Apple Silicon·Intel입니다. 설치 파일에는 Python 서비스가 포함됩니다. Python이나 Go를 사용자가 따로 설치할 필요가 없습니다.

[GitHub Releases](https://github.com/nimo0711/forkrail/releases)에서 운영체제에 맞는 파일을 받습니다. 저장소가 비공개이면 접근 권한이 있는 GitHub 계정으로 로그인해야 합니다. 아직 게시된 릴리즈가 없다면 [Actions의 설치 후보](https://github.com/nimo0711/forkrail/actions)에서 시험 결과와 파일을 확인합니다.

| 환경 | 설치 파일 | 실행 |
| --- | --- | --- |
| Mac Apple Silicon | `Forkrail-<버전>-macos-arm64.pkg` | 응용 프로그램의 Forkrail |
| Mac Intel | `Forkrail-<버전>-macos-x86_64.pkg` | 응용 프로그램의 Forkrail |
| Windows x64 | `Forkrail-<버전>-windows-x64-Setup.exe` | 시작 메뉴의 Forkrail |

Windows에는 Microsoft WebView2 Runtime이 필요합니다. 없으면 설치 프로그램이 Microsoft 설치 페이지를 안내합니다. 각 OS의 ZIP도 제공하며, 안의 폴더 전체를 유지해야 합니다.

**서명되지 않은 설치 후보와 정식 서명 배포본은 구분합니다.** 후보 파일에는 `release-manifest.json`의 `signed: false`가 표시됩니다. 이런 파일은 macOS Gatekeeper나 Windows SmartScreen에서 차단될 수 있습니다. 보안 설정을 끄는 설치 절차는 제공하지 않습니다.

## 앱 동작

앱을 열면 필요한 로컬 서비스를 함께 시작합니다. 앱이 완전히 종료되면 자신이 시작한 서비스도 종료합니다. 이미 같은 OS 계정의 호환 작업 보드가 실행 중이면 그 서비스와 기존 데이터를 사용하며, 앱 종료 시 기존 서비스는 그대로 둡니다. 연결했던 서비스가 종료되거나 교체되면 연결을 중단합니다. 서비스를 다시 시작한 뒤 앱도 다시 열어 주세요. 기본 주소는 <http://127.0.0.1:8790>입니다.

- Claude·Codex 세션과 하위 평가 세션의 관계
- Jira 스토리·태스크 또는 Jira 없는 작업 묶음 기준 진척
- 요청·결과·파일 변경·산출물·진행 기록
- 로컬 개발 서버와 터미널 연결
- 설치된 스킬 선택, 요청 내용 입력, 실행
- Claude·Codex 사용량과 도구 실행 결과
- Jira·GitLab·로컬 Git 설정과 AI용 도움말

Jira·GitLab은 설정에서 연결하며 `acli`·`glab`에 로그인한 계정을 사용합니다. 인증 토큰을 보드에 직접 입력하지 않습니다. CLI와 AI 도구는 선택 연동이므로 별도 설치가 필요합니다. 제목 요약을 켜면 Claude CLI 사용량이 발생할 수 있습니다.

Windows는 로컬 Windows 사용자 기록을 읽습니다. WSL 내부 세션은 자동으로 연결하지 않습니다. 현재 세션에 직접 스킬을 실행하는 기능은 인증된 연결을 확인할 수 있는 환경에서만 사용할 수 있습니다. Windows에서는 새 터미널 실행을 사용하며, 연결할 수 없는 이유를 화면에 표시합니다.

## 기록과 설정

| 실행 방식 | 데이터 위치 |
| --- | --- |
| 설치본 · Mac | `~/Library/Application Support/Forkrail` |
| 설치본 · Windows | `%LOCALAPPDATA%/Forkrail` |
| 소스에서 실행 | 저장소 폴더 |

이 폴더에 작업 DB, 연결·바로가기 설정, 요약 캐시를 저장합니다. 앱 업데이트나 제거는 이 데이터를 지우지 않습니다. AI 기록은 현재 사용자의 `.claude`, `.codex`에서 읽으며 설치 파일에 포함하지 않습니다.

기존 소스 실행 데이터를 옮기려면 설치 앱을 처음 열기 전에 [데이터 이전 절차](docs/releases.md#기존-소스-실행에서-설치본으로-이전)를 따릅니다. 아직 존재하지 않는 새 데이터 폴더로 복사하며 기존 원본과 대상 데이터를 덮어쓰지 않습니다.

| 환경 변수 | 용도 |
| --- | --- |
| `FORKRAIL_DATA_DIR` | 데이터 폴더 명시 지정 |
| `WORK_BOARD_PORT` | 로컬 서비스 포트, 기본 `8790` |
| `WORK_BOARD_DB` | 작업 DB 경로 명시 지정 |
| `WORK_BOARD_URL` | `wb` 명령의 전송 대상 |
| `WORK_BOARD_SPOOL` | 서버가 받지 못한 기록의 보관 위치 |

## 소스에서 실행

Python 3.12를 권장합니다. Windows의 프로세스 탐지에는 psutil이 필요합니다.

```sh
git clone https://github.com/nimo0711/forkrail.git
cd forkrail
python -m pip install psutil==7.2.2
python server.py
```

서비스 실행 후 브라우저에서 <http://127.0.0.1:8790>을 엽니다. 기존 서비스가 이 포트에서 실행 중이면 중복으로 시작하지 않습니다. 직접 작업 기록을 남기는 방법은 [AGENT_GUIDE.md](AGENT_GUIDE.md)에 있습니다.

macOS의 기존 Swift 창은 개발·복구용으로 유지합니다. `bash scripts/build_app.sh`로 만들며 이 창은 서버를 별도로 실행해야 합니다.

## 검증과 릴리즈

```sh
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.cjs
cd desktop
go test ./...
```

[릴리즈 가이드](docs/releases.md)에 빌드 환경, 설치 후보 생성, 서명·공증 설정, GitHub draft 릴리즈 절차와 검증 범위를 정리했습니다. 버전 정본은 `release.json`입니다. 기존 Swift 개발 창의 버전과 배포 버전은 구분합니다.

## 구조

- `desktop/`: Go/Wails 공통 창, 서비스 관리, 알림, 프록시
- `desktop_service.py`, `runtime_paths.py`, `platform_support.py`: 배포 실행과 OS 호환
- `server.py`, `index.html`, `stories.html`: 로컬 HTTP 서비스와 보드 화면
- `scan.py`, `usage.py`: 세션·사용량 읽기
- `connections.py`, `board_actions.py`: 연결·스킬·바로가기
- `wbcore.py`, `claude_hook.py`: 작업 기록과 Claude 훅
- `scripts/`, `packaging/`, `.github/workflows/`: 빌드·설치·검증·릴리즈
- `app/`: Signal 아이콘 정본과 기존 Swift 개발 창

로컬 사용량은 확인 가능한 기록의 합계이며 서비스 청구서나 계정 한도와 다를 수 있습니다. 세션 감지는 AI 도구의 로컬 기록 형식과 사용 중인 환경에 영향을 받습니다.
