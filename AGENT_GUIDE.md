# Huginn 기록

소스에서 실행할 때는 저장소의 `wb` 명령을 사용합니다. 설치 앱에는 Python 없이 동작하는 같은 명령이 포함됩니다.

- macOS 설치 앱: `"/Applications/Huginn.app/Contents/Resources/service/huginn-service" wb`
- Windows 설치 앱(PowerShell): `& "$env:LOCALAPPDATA\Programs\Huginn\service\huginn-service.exe" wb`
- ZIP을 다른 폴더에 풀었거나 설치 위치를 바꾸었다면 해당 폴더의 `service` 실행 파일 경로를 사용합니다.

아래 예시는 소스 저장소 안에서 실행합니다. 설치 앱에서는 `./wb`를 위 명령으로 바꾸고, 다른 작업 폴더에서는 실행 파일의 전체 경로를 사용하세요.

```sh
./wb story '작업 제목' --story huginn
./wb status '검증' '진행 중' --title '변경 동작 확인' --story huginn
./wb log '검증 결과 한 줄' --item '검증' --story huginn
./wb status '검증' '완료' -m '확인한 결과' --story huginn
```

- Jira 키가 작업 브랜치에 있으면 자동으로 사용합니다. 없으면 `--story`에 작업 묶음 키를 지정합니다.
- 전후 측정은 `./wb metric '<항목>' '<측정 이름>' <전> <후> -m '<조건·단위>'`로 기록합니다.
- 머지·배포까지 완료한 뒤 전체 작업을 완료 처리합니다. 구현 검증만 끝났다면 해당 항목만 완료합니다.
- 서버에 연결하지 못해도 기록을 보관하며, 서버가 이후 가져갑니다. 설치 앱의 기본 위치는 macOS `~/Library/Application Support/Huginn/spool`, Windows `%LOCALAPPDATA%/Huginn/spool`입니다. 소스 실행은 기존 `/tmp/work-board-spool`을 사용합니다. 데이터 위치를 지정했다면 같은 설정으로 명령을 실행합니다.
- 연결과 스킬을 등록하려면 설정의 **AI용 도움말**을 복사해 사용합니다. 실제 설치 경로와 사용 가능한 명령이 들어 있습니다.
