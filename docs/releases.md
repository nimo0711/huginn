# Forkrail 릴리즈 가이드

설치 후보를 만든 뒤 실제 동작을 검증하고, 서명·공증을 거쳐 GitHub에 배포하는 절차입니다. 버전과 지원 대상은 `release.json`에서 관리합니다. HyperWarp 설치본에서 확인한 Wails·OS WebView·macOS pkg 방식을 참고했으며, 아래 빌드 시스템은 Forkrail용으로 구성했습니다.

## 빌드 환경

| 대상 | 빌드 환경 | 결과 |
| --- | --- | --- |
| `macos-arm64` | Apple Silicon Mac / Actions `macos-15` | pkg, zip |
| `macos-x86_64` | Intel Mac / Actions `macos-15-intel` | pkg, zip |
| `windows-x64` | Windows x64 / Actions `windows-2022` | Setup.exe, zip |

Go 1.27.4, Python 3.12, Node.js 22가 필요합니다. Mac에는 Xcode Command Line Tools, Windows에는 NSIS와 WebView2 Runtime도 필요합니다. Wails v2.16.0과 Python 빌드 의존성은 고정 버전을 사용합니다.

Python 실행 파일과 네이티브 의존성은 대상 OS·CPU에서 빌드합니다. 맥에서 Windows용 Python 실행 파일을 만들지 않습니다. CI는 세 환경에서 각각 실행하며 소스·설치 결과를 시험합니다.

## 로컬 설치 후보 만들기

저장소에서 가상환경을 만들고 의존성을 설치합니다.

```sh
python -m venv .release-venv
# macOS
.release-venv/bin/python -m pip install -r requirements-release.txt
.release-venv/bin/python scripts/build_release.py
# Windows PowerShell에서는 위 두 줄 대신:
# .release-venv\Scripts\python.exe -m pip install -r requirements-release.txt
# .release-venv\Scripts\python.exe scripts/build_release.py
```

현재 OS·CPU에 맞는 패키지가 `dist/<대상>/`에 생성됩니다. 빌드는 이 대상의 이전 생성물을 교체하므로, 보관할 후보는 다른 폴더로 복사하세요. 실행 중인 기존 Forkrail 앱·서버를 설치하거나 교체하지 않습니다.

검증 예:

```sh
python scripts/verify_release.py dist/macos-arm64
python scripts/smoke_release.py --target macos-arm64
```

두 번째 명령은 임시 홈·임시 데이터·임시 포트로 실제 패키지 서비스와 앱 창을 실행합니다. 로컬 Mac에서는 pkg 내용을 풀어 검사하고 `/Applications`는 변경하지 않습니다. GitHub의 일회용 Mac 러너에서는 pkg를 실제 설치한 후 시험합니다. Windows 러너에서는 사용자 단위 설치·실행·제거와 데이터 보존도 확인합니다.

생성물:

- 운영체제별 설치 파일과 ZIP
- `SHA256SUMS`: 설치 파일·ZIP·릴리즈 메타데이터의 SHA-256
- `release-manifest.json`: 버전·플랫폼·서명 여부·소스 커밋·미커밋 변경 여부
- `smoke-result.json`: 패키지와 실제 WebView 시험 결과

후보는 기본적으로 서명되지 않습니다. 실제 사용자 컴퓨터에서의 Gatekeeper·SmartScreen 허용이나 공증을 통과했다는 뜻은 아닙니다. Mac 빌드는 앱 안의 모든 네이티브 실행 파일·라이브러리가 macOS 13 이하를 최소 버전으로 요구하는지 확인합니다. 로컬 Python이 더 높은 OS 버전을 요구하면 호환되는 배포용 Python으로 빌드해야 합니다. 공식 후보는 지정한 CI 환경에서 만듭니다.

## GitHub에서 빌드

개발 브랜치 push와 버전 태그 push는 자동 빌드를 시작합니다. 세 환경의 Python·JavaScript·Go 시험, 내장 서비스 시험, 설치 파일 검증, 실제 WebView 시험 중 하나라도 실패하면 성공 릴리즈로 취급하지 않습니다. 결과는 Actions의 `Forkrail-<대상>` 아티팩트에서 받습니다. 보관 기간은 14일입니다.

`Build Forkrail installers` 워크플로가 기본 브랜치에 반영된 뒤에는 **Run workflow**로도 실행할 수 있습니다.

1. `release.json`의 버전을 변경하고 검증합니다. 예: `1.2.0-rc.1`.
2. 검증한 커밋에 같은 버전의 태그 `v1.2.0-rc.1`을 만들어 GitHub에 push합니다. 태그 push의 자동 후보 빌드와 별도로, 서명·draft 배포는 다음 수동 실행으로 진행합니다.
3. Run workflow에서 해당 태그를 선택하고 `tag`에도 같은 값을 입력합니다.
4. 서명 배포라면 `signed`를 켭니다. 아래 자격 정보가 필요합니다.
5. `publish_draft`를 켜면 모든 플랫폼 성공 후 설치 파일과 증거를 draft 릴리즈에 첨부합니다.
6. 파일·시험 결과·서명 상태·릴리즈 설명을 검토한 뒤 사람이 GitHub에서 Publish release를 누릅니다.

워크플로는 릴리즈를 자동 공개하지 않습니다. 태그와 `release.json` 버전, 실행 커밋이 맞아야 합니다. 세 플랫폼 모두 해당 커밋에서 미커밋 변경 없이 빌드했고 설치·서비스·화면 시험을 통과해야 합니다. draft에는 대상별 이름으로 메타데이터와 검증 결과를 첨부하고, 실제 다운로드 파일명에 맞는 `<대상>-SHA256SUMS`를 제공합니다. 이미 같은 릴리즈가 있으면 덮어쓰지 않고 중단합니다. 정식 버전(dash 없는 버전)의 draft에는 서명된 설치 파일이 필요합니다.

GitHub 인증의 `repo` 권한 외에 `.github/workflows` 변경에는 `workflow` 권한이 필요할 수 있습니다. 저장소를 공개로 바꾸는 작업은 이 절차에 포함되지 않습니다. 비공개 저장소의 Actions는 계정의 포함 실행 시간을 사용합니다.

## 서명과 공증

인증서는 소스나 배포 파일에 넣지 않습니다. 설치 후보의 시험 통과와 서명 배포 준비 완료를 구분합니다.

Mac의 GitHub Actions secrets:

| 이름 | 값 |
| --- | --- |
| `APPLE_CERTIFICATE_BASE64` | Developer ID Application 인증서와 개인 키를 포함한 p12의 base64 |
| `APPLE_CERTIFICATE_PASSWORD` | 위 p12 비밀번호 |
| `APPLE_INSTALLER_CERTIFICATE_BASE64` | Developer ID Installer p12의 base64 |
| `APPLE_INSTALLER_CERTIFICATE_PASSWORD` | 위 p12 비밀번호 |
| `APPLE_APP_IDENTITY` | Developer ID Application의 정확한 서명 이름 |
| `APPLE_INSTALLER_IDENTITY` | Developer ID Installer의 정확한 서명 이름 |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | 공증용 Apple 계정·팀·앱 전용 암호 |

CI는 일회용 키체인에서 내부 실행 파일→앱→pkg 순서로 서명하고, pkg와 ZIP에 들어갈 앱을 공증합니다. 완료 후 인증 자료를 제거합니다. 로컬에서는 기존 키체인에 필요한 ID와 공증 프로필을 직접 등록한 뒤 `FORKRAIL_APPLE_APP_IDENTITY`, `FORKRAIL_APPLE_INSTALLER_IDENTITY`, `FORKRAIL_NOTARY_PROFILE`을 지정하고 `build_release.py --signed`를 실행합니다. 별도 키체인이라면 `FORKRAIL_NOTARY_KEYCHAIN`도 지정합니다.

Windows는 `WINDOWS_CERTIFICATE_BASE64`, `WINDOWS_CERTIFICATE_PASSWORD` secrets로 pfx 기반 서명을 지원합니다. 앱·내장 서비스·설치 파일에 서명한 뒤 SignTool로 확인합니다. 하드웨어 토큰이나 클라우드 서명 서비스만 제공하는 인증서는 해당 공급자의 방식으로 별도 연동해야 합니다. 로컬 빌드는 `FORKRAIL_WINDOWS_CERT_FILE`, `FORKRAIL_WINDOWS_CERT_PASSWORD`, 필요하면 `FORKRAIL_SIGNTOOL`을 지정합니다.

현재 인증서가 없으면 unsigned 후보까지만 검증할 수 있습니다. 보안 경고를 우회하거나 다른 앱의 서명을 재사용하지 않습니다.

## 기존 소스 실행에서 설치본으로 이전

이미 8790에서 실행 중인 호환 보드가 있으면 새 앱도 그 서비스를 사용합니다. 새 앱이 기존 서비스나 LaunchAgent를 자동으로 중지하지 않습니다.

기존 데이터를 이전하려면 설치 후 앱을 처음 실행하기 전에 진행합니다. 설치본이 자체 서비스를 실행하도록 전환하려면 먼저 기존 소스 폴더의 데이터를 새 폴더로 복사합니다. 데이터 대상 폴더는 아직 존재하지 않아야 합니다. 이미 앱을 열어 폴더가 생겼다면 앱을 종료하고 해당 폴더를 별도 이름으로 백업한 뒤 이전합니다. 새 기록이 있으면 자동 병합되지 않으므로 보존할 데이터를 먼저 확인하세요. 아래 `<기존 보드 폴더>`는 본인 환경의 실제 경로로 바꿉니다.

Mac:

```sh
'/Applications/Forkrail.app/Contents/Resources/service/forkrail-service' migrate \
  --from '<기존 보드 폴더>' \
  --data-dir "$HOME/Library/Application Support/Forkrail"
```

Windows PowerShell:

```powershell
& "$env:LOCALAPPDATA\Programs\Forkrail\service\forkrail-service.exe" migrate `
  --from '<기존 보드 폴더>' `
  --data-dir "$env:LOCALAPPDATA\Forkrail"
```

SQLite의 backup 기능으로 DB를 복사하고 연결·바로가기·앱 설정만 이전합니다. 원본을 수정하지 않으며 실행 중인 요청·소켓·임시 명령 파일은 복사하지 않습니다. 이미 대상 데이터가 있으면 중단합니다.

복사 후 사용자가 기존 보드의 자동 실행을 해제하고 서비스를 종료한 다음 새 앱을 실행합니다. 이전 중 기존 보드에 추가로 기록한 내용은 복사본에 반영되지 않으므로, 최종 이전은 기존 작업이 멈춘 시점에 수행하세요. 포트 충돌 시 다른 프로세스를 임의로 종료하지 않습니다.

## 검증 범위

자동 시험은 빈 사용자 환경에서의 설치/실행/종료, 데이터 보존, API 보호, 화면 로드와 작업 명령 전달 형식을 확인합니다. 실제 계정으로 Jira·GitLab에 로그인하거나 실제 AI 세션에 스킬을 실행하지는 않습니다. Windows의 인증된 현재 세션 연결·터미널 포커스·WSL 기록 연결은 지원 범위에 포함되지 않으며 화면에서 사유를 표시합니다.

마지막 실행의 실제 통과 여부는 Actions 결과와 `smoke-result.json`을 확인합니다. 구성 파일이 존재한다는 이유만으로 Windows 설치 검증·서명·공증이 끝났다고 보지 않습니다.

참고: [Wails 설치 환경](https://v2.wails.io/docs/gettingstarted/installation/) · [PyInstaller의 OS별 빌드](https://www.pyinstaller.org/en/stable/) · [Microsoft WebView2 배포](https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution) · [GitHub 러너](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
