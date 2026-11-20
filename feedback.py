import platform
import re
import subprocess
from urllib.parse import urlencode

from platform_support import cli_path

KINDS = {'bug': '버그', 'idea': '의견'}
REPOSITORY = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*')
BROWSER_URL_LIMIT = 6000


def compose(kind, title, body, version, os_name):
    if kind not in KINDS:
        raise ValueError('의견 종류를 선택해 주세요.')
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError('제목과 내용을 입력해 주세요.')
    title, body = title.strip(), body.strip()
    if not 1 <= len(title) <= 120:
        raise ValueError('제목은 1~120자로 입력해 주세요.')
    if not 1 <= len(body) <= 5000:
        raise ValueError('내용은 1~5000자로 입력해 주세요.')
    footer = f'\n\n---\n앱 버전: {version}\nOS: {os_name} ({platform.machine() or "알 수 없음"})\n보낸 곳: Huginn 의견 보내기'
    return f'[{KINDS[kind]}] {title}', body + footer


def browser_url(repository, title, body):
    url = f'https://github.com/{repository}/issues/new?' + urlencode({'title': title, 'body': body})
    while len(url) > BROWSER_URL_LIMIT and len(body) > 200:
        body = body[:int(len(body) * 0.8)].rstrip() + '\n\n(내용이 길어 앞부분만 채웠습니다. 나머지는 붙여 넣어 주세요.)'
        url = f'https://github.com/{repository}/issues/new?' + urlencode({'title': title, 'body': body})
    return url


def submit(repository, kind, title, body, version, os_name, runner=subprocess.run, gh=None):
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValueError('의견을 받을 저장소가 설정되어 있지 않습니다.')
    title, body = compose(kind, title, body, version, os_name)
    fallback = {'ok': True, 'status': 'browser', 'url': browser_url(repository, title, body)}
    gh = gh if gh is not None else cli_path('gh')
    if not gh:
        return {**fallback, 'message': '이 컴퓨터에 GitHub CLI가 없어 작성 페이지로 보냅니다. 열어서 등록을 눌러 주세요.'}
    try:
        result = runner([gh, 'issue', 'create', '-R', repository, '--title', title, '--body-file', '-'],
                        input=body, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        result = None
    created = result is not None and result.returncode == 0 and re.search(
        r'https://github\.com/' + re.escape(repository) + r'/issues/\d+', result.stdout or '')
    if not created:
        return {**fallback, 'message': 'GitHub 로그인으로 바로 등록하지 못해 작성 페이지로 보냅니다. 열어서 등록을 눌러 주세요.'}
    return {'ok': True, 'status': 'created', 'url': created.group(0), 'message': '의견을 등록했습니다. 고맙습니다.'}
