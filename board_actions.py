"""User-clicked actions, backed by installed skills and verified session context."""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from session_delivery import SessionDelivery, validate_prompt, prompt_hash, skill_request_text
from connections import Connections
from platform_support import (IS_WINDOWS, file_lock, powershell_command, replace_file,
                              windows_agent_command, write_powershell_launcher)
from runtime_paths import FROZEN, RESOURCE_ROOT
try:
    import tomllib
except ImportError:
    try:import tomli as tomllib
    except ImportError:tomllib=None


def skill_rules(home,cwd):
    rules=[]
    for path in [home/'.codex/config.toml',cwd/'.codex/config.toml']:
        if not path.is_file():continue
        # Never bypass a disabled skill when configuration cannot be read.
        try:
            if tomllib is None:return None
            config=tomllib.loads(path.read_text(encoding='utf-8'))
            rules.extend(config.get('skills',{}).get('config',[]))
        except (OSError,ValueError,TypeError):return None
    return rules


def skill_enabled(path,name,rules):
    if rules is None:return False
    enabled=True
    for rule in rules:
        if not isinstance(rule,dict):continue
        selector=rule.get('path')
        match=rule.get('name')==name
        if isinstance(selector,str):
            target=Path(selector).expanduser().resolve()
            match=target in (path.resolve(),path.parent.resolve())
        if match and isinstance(rule.get('enabled'),bool):enabled=rule['enabled']
    return enabled


def read_json(path,default=None):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):return {} if default is None else default


def metadata(path):
    try:
        text=path.read_text(encoding='utf-8')[:65536]
    except (OSError,UnicodeError):return {}
    if not text.startswith('---'):return {}
    parts=text.split('---',2)
    if len(parts)<3:return {}
    result={}
    for line in parts[1].splitlines():
        match=re.match(r'^([\w-]+):\s*(.*)',line)
        if match:result[match[1]]=re.sub(r'\s+#.*$','',match[2]).strip().strip('"\'')
    return result


def powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def executable(agent):
    if IS_WINDOWS:
        return next(iter(windows_agent_command(agent)), None)
    home=Path.home()
    candidates=([home/'.local/bin/claude'] if agent=='claude' else sorted(home.glob('.nvm/versions/node/*/bin/codex'),key=lambda p:p.stat().st_mtime,reverse=True))
    candidates += [Path(p) for p in [shutil.which(agent),'/opt/homebrew/bin/'+agent,'/usr/local/bin/'+agent] if p]
    return next((str(p) for p in candidates if p.is_file() and os.access(p,os.X_OK)),None)


def agent_command(agent, home):
    if IS_WINDOWS:
        return windows_agent_command(agent, home)
    cli = executable(agent)
    return [cli] if cli else []


def open_terminal(path,app=''):
    if IS_WINDOWS:
        subprocess.Popen(powershell_command(path), creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    args=['/usr/bin/open']+(['-a',app] if app else [])+[str(path)]
    result=subprocess.run(args,capture_output=True,text=True,timeout=10)
    if result.returncode:raise RuntimeError('새 터미널 창을 열지 못했습니다.')


class ActionStore:
    def __init__(self,state,home=None):
        self.state=Path(state);self.home=Path(home or Path.home());self.lock=threading.Lock()
        self.prefs=self.state/'favorites.json';self.requests={}
        self.delivery=SessionDelivery(self.state,self.home)
        self.connections=Connections(self.state,self.home)

    def editing(self):
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        return file_lock(self.state / '.lock')

    def save(self,path,value):
        tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
        try:
            with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as f:
                json.dump(value,f,ensure_ascii=False)
            replace_file(tmp, path)
        finally:tmp.unlink(missing_ok=True)

    def settings(self):
        result={'auto_servers':True,'auto_terminal':True,'launch_app':''}
        saved=read_json(self.state/'settings.json')
        if isinstance(saved,dict):
            for k,v in result.items():
                if k in saved and type(saved[k]) is type(v):result[k]=saved[k]
        return result

    def configure(self,changes):
        if not isinstance(changes,dict):raise ValueError('설정 형식이 올바르지 않습니다.')
        defaults={'auto_servers':True,'auto_terminal':True,'launch_app':''}
        for key,value in changes.items():
            if key not in defaults or type(value) is not type(defaults[key]):raise ValueError('지원하지 않는 설정 값입니다.')
            if key=='launch_app' and value:
                if IS_WINDOWS:
                    raise ValueError('Windows에서는 기본 PowerShell 터미널을 사용합니다. 시스템 기본값을 선택해 주세요.')
                app=Path(value).expanduser()
                if not app.is_absolute() or app.suffix!='.app' or not (app/'Contents/Info.plist').is_file():raise ValueError('설치된 터미널 앱의 전체 .app 경로를 입력해 주세요.')
                changes={**changes,'launch_app':str(app.resolve())}
        with self.editing():
            result={**self.settings(),**changes};self.save(self.state/'settings.json',result)
        return result

    def shortcuts(self,cwd):
        catalog={s['id']:s for s in self.catalog(cwd)};values=read_json(self.prefs)
        return [{**catalog.get(key,{'id':key,'name':key,'available':False,'agent':key.split(':')[0]}),
                 'label':value.get('label') or catalog.get(key,{}).get('label') or key}
                for key,value in values.items() if isinstance(value,dict)]

    def guide(self,cwd):
        if IS_WINDOWS:
            folder = powershell_quote(cwd)
            command = [sys.executable, 'wb'] if FROZEN else [sys.executable, RESOURCE_ROOT / 'wb']
            wb = '& ' + ' '.join(powershell_quote(arg) for arg in command)
            terminal_help = '새 터미널을 선택하면 기본 PowerShell 창에서 실행합니다.'
        else:
            folder = shlex.quote(str(cwd))
            wb = shlex.quote(sys.executable) + ' wb' if FROZEN else shlex.quote(str(RESOURCE_ROOT / 'wb'))
            terminal_help = (
                '새 터미널을 선택하면 macOS의 .command 기본 앱을 사용합니다. 별도 앱은 전체 경로로 설정하세요.\n'
                f"{wb} actions configure --launch-app '/Applications/<터미널 앱>.app'"
            )
        return self.connections.guide(wb)+'\n\n'+f'''Huginn 연결·바로가기 등록 안내

이 도움말은 Claude와 Codex 모두 사용할 수 있습니다. 사용자가 원하는 버튼과 실행 위치를 확인한 뒤 등록하세요. 등록은 스킬을 실행하지 않습니다.
Huginn의 검색 범위는 Claude·Codex 로컬 스킬, Codex 시스템 스킬, 활성화된 Claude 플러그인입니다. Codex 플러그인 스킬은 현재 Huginn이 활성 여부를 확인하지 못하므로 연결된 Codex 세션에서 실행하세요. 캐시 폴더만 보고 활성화된 스킬로 등록하지 마세요.

1. 설치·활성화된 스킬의 정확한 ID 조회:
{wb} actions skills --cwd {folder}
2. 기존 등록과 연결 설정 조회:
{wb} actions list --cwd {folder}
3. 조회한 ID로 바로가기 추가 또는 이름 변경:
{wb} actions add --skill '<조회한 ID>' --label '<사용자가 원하는 이름>' --cwd {folder}
4. 요청받은 바로가기만 제거:
{wb} actions remove --skill '<등록된 ID>'
5. 서버·터미널 자동 연결 설정:
{wb} actions configure --auto-servers on --auto-terminal on
6. 세션 화면에서 스킬을 선택한 뒤 요청 내용을 함께 입력하고 실행할 수 있습니다. 기본 실행 위치는 현재 세션입니다. 연결이 끊기면 새 세션으로 자동 실행하지 않습니다. {terminal_help}
기본 앱으로 복원: {wb} actions configure --launch-app system

마지막으로 actions list를 다시 실행해 저장 결과를 확인하세요. 설정 화면과 세션 버튼에 자동 반영됩니다. 버튼 이름·스킬·앱을 추가하기 위해 제품 소스를 수정하지 마세요. 다른 등록을 임의로 지우지 말고, 사용자가 실행을 요청하지 않았다면 스킬을 실행하지 마세요. 허용된 로컬 명령 실행 권한과 파일 권한을 그대로 따르세요.'''

    def catalog(self,cwd):
        cwd=Path(cwd);found={};seen=set();favorites=read_json(self.prefs);rules=skill_rules(self.home,cwd)
        def add(path,agent,scope,kind='skill',namespace='',origin=None):
            key=(agent,str(path.resolve()))
            if key in seen:return
            seen.add(key)
            meta=metadata(path)
            if not meta or meta.get('user-invocable','true').lower() not in ('true','yes','on'):return
            name=meta.get('name') or (path.parent.name if kind=='skill' else path.stem)
            if not re.fullmatch(r'[\w:.-]{1,120}',name):return
            if agent=='codex' and not skill_enabled(path,name,rules):return
            identity=hashlib.sha256(str(origin or path.resolve()).encode()).hexdigest()[:16]
            identifier=f'{agent}:{scope}:{namespace}:{kind}:{name}:{identity}'
            label=favorites.get(identifier,{}).get('label') or name
            found[identifier]={'id':identifier,'name':name,'label':label,'agent':agent,'kind':kind,
                'description':meta.get('description','')[:240],'path':str(path.resolve()),
                'favorite':bool(favorites.get(identifier)), 'available':bool(executable(agent))}
        for agent,roots in [('claude',[(self.home/'.claude/skills','user'),(cwd/'.claude/skills','project')]),
                            ('codex',[(self.home/'.agents/skills','user'),(self.home/'.codex/skills','user'),(self.home/'.codex/skills/.system','system'),(cwd/'.agents/skills','project'),(cwd/'.codex/skills','project')])]:
            for root,scope in roots:
                for path in sorted(root.glob('*/SKILL.md')):
                    add(path,agent,scope)
        settings={}
        for path in [self.home/'.claude/settings.json',self.home/'.claude/settings.local.json',cwd/'.claude/settings.json',cwd/'.claude/settings.local.json']:
            settings.update(read_json(path).get('enabledPlugins',{}))
        installed=read_json(self.home/'.claude/plugins/installed_plugins.json').get('plugins',{})
        for plugin,records in installed.items():
            if settings.get(plugin) is not True:continue
            if not isinstance(records,list):continue
            for record in records:
                if record.get('scope') not in ('user','project','local'):continue
                if record.get('scope')!='user' and Path(record.get('projectPath','/')).resolve()!=cwd.resolve():continue
                root=Path(record.get('installPath','/nonexistent'))
                owner=str(Path(record['projectPath']).resolve()) if record.get('scope')!='user' else str(self.home.resolve())
                for kind,pattern in [('skill','skills/*/SKILL.md'),('command','commands/*.md')]:
                    for path in sorted(root.glob(pattern)):add(path,'claude',record['scope'],kind,plugin,owner+'|'+plugin+'|'+str(path.relative_to(root)))
        return sorted(found.values(),key=lambda s:(not s['favorite'],s['label'].casefold(),s['agent']))

    def favorite(self,identifier,enabled,cwd,label=None):
        if not isinstance(identifier,str) or not 1<=len(identifier)<=500:raise ValueError('스킬 ID가 올바르지 않습니다.')
        if enabled and identifier not in {s['id'] for s in self.catalog(cwd)}:raise ValueError('설치된 스킬을 찾을 수 없습니다.')
        with self.editing():
            values=read_json(self.prefs)
            if enabled:values[identifier]={'label':str(label if label is not None else values.get(identifier,{}).get('label',''))[:60]}
            else:values.pop(identifier,None)
            self.save(self.prefs,values)

    def deliver(self,identifier,session,request_id,prompt=""):
        item=next((s for s in self.catalog(session['cwd_path']) if s['id']==identifier),None)
        if not item:raise ValueError('스킬이 제거되었거나 활성화되지 않았습니다.')
        return self.delivery.send(session,item,request_id,prompt)

    def write_launcher(self, directory, identifier, args, cwd, *, env=None, label=''):
        note = '이 컴퓨터의 CLI 로그인 정보를 변경합니다. 인증을 마치면 Huginn의 연결 확인을 눌러 주세요.'
        if IS_WINDOWS:
            path = directory / (identifier + '.json')
            write_powershell_launcher(path, args, cwd, env=env, label=f'{label}\n{note}' if label else '',
                                      remove_agent_context=not label)
            return path

        path = directory / (identifier + '.command')
        tool_path = os.pathsep.join(dict.fromkeys([
            str(Path(args[0]).parent), str(self.home / '.local/bin'), '/opt/homebrew/bin',
            os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'),
        ]))
        lines = ['#!/bin/zsh', 'cd -- ' + shlex.quote(str(cwd)) + ' || exit 1', 'export PATH=' + shlex.quote(tool_path)]
        if label:
            lines.extend('export ' + key + '=' + shlex.quote(value) for key, value in (env or {}).items())
            lines.extend([
                'printf "%s\\n" ' + shlex.quote(label), 'printf "%s\\n" ' + shlex.quote(note),
                shlex.join(args), 'result=$?',
                'printf "%s\\n" ' + shlex.quote('Huginn으로 돌아가 연결 확인을 눌러 주세요. 이 창은 닫아도 됩니다.'),
                'exit "$result"',
            ])
        else:
            lines.append('exec /usr/bin/env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CODEX_THREAD_ID ' + shlex.join(args))
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        path.chmod(0o700)
        return path

    def login(self,kind,request_id,switch=False):
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}',request_id):
            raise ValueError('로그인 요청을 확인할 수 없습니다.')
        if kind not in ('jira','gitlab') or type(switch) is not bool:
            raise ValueError('로그인할 연결 종류를 확인해 주세요.')
        args,cwd,env,label=self.connections.login_command(kind,switch)
        if not Path(args[0]).is_absolute() or not Path(args[0]).is_file() or not os.access(args[0],os.X_OK):
            raise ValueError('로그인에 필요한 CLI가 없습니다. 연결의 설치 안내에서 설치한 뒤 다시 시도해 주세요.')
        with self.lock:
            now=time.time();signature=('login',kind,request_id)
            self.requests={k:v for k,v in self.requests.items() if now-v['at']<600}
            if signature in self.requests:return self.requests[signature]
            directory=self.state/'launchers';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            launcher = self.write_launcher(directory, str(uuid.uuid4()), args, cwd, env=env, label=label)
            try:open_terminal(launcher,self.settings()['launch_app'])
            except Exception:
                launcher.unlink(missing_ok=True);raise
            result={'ok':True,'status':'login_opened','at':now,'message':'로그인 창을 열었습니다. 인증을 마친 뒤 연결 확인을 눌러 주세요.'}
            self.requests[signature]=result
            return result

    def launch(self,identifier,cwd,request_id,prompt=""):
        prompt=validate_prompt(prompt);fingerprint=prompt_hash(prompt)
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}',request_id):raise ValueError('실행 요청을 확인할 수 없습니다.')
        cwd=Path(cwd).resolve()
        if not cwd.is_dir():raise ValueError('작업 폴더가 없습니다.')
        with self.lock,self.editing():
            now=time.time();records_path=self.state/'launch-requests.json'
            try:records=json.loads(records_path.read_text(encoding='utf-8')) if records_path.exists() else {}
            except (OSError,ValueError):raise ValueError('이전 실행 기록을 읽지 못했습니다. 새 터미널이 이미 열렸는지 확인해 주세요.')
            if not isinstance(records,dict):raise ValueError('이전 실행 기록을 확인할 수 없습니다.')
            signature=hashlib.sha256(json.dumps([identifier,str(cwd),request_id]).encode()).hexdigest()
            if signature in records and records[signature].get('status')!='failed':
                previous=records[signature]
                if previous.get('prompt_hash',prompt_hash(''))!=fingerprint:raise ValueError('요청 내용이 바뀌었습니다. 새 실행 요청을 사용해 주세요.')
                if previous.get('status')=='pending':
                    previous={**previous,'status':'unknown','message':'이전 실행 창이 열렸는지 확인하지 못했습니다. 터미널을 먼저 확인해 주세요.'}
                return previous
            item=next((s for s in self.catalog(cwd) if s['id']==identifier),None)
            if not item:raise ValueError('스킬이 제거되었거나 활성화되지 않았습니다.')
            command = agent_command(item['agent'], self.home)
            if not command:raise ValueError('실행할 '+item['agent']+' CLI가 없습니다.')
            ident=str(uuid.uuid4())
            prompt=skill_request_text(item,prompt,current=False)
            args=([*command,'--session-id',ident,'--name',item['label'],prompt] if item['agent']=='claude' else [*command,'-C',str(cwd),prompt])
            directory=self.state/'launchers';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            launcher = self.write_launcher(directory, ident, args, cwd)
            result={'id':ident,'status':'pending','at':now,'label':item['label'],'agent':item['agent'],'launcher':str(launcher),'prompt_hash':fingerprint}
            records[signature]=result;self.save(records_path,records)
            try:open_terminal(launcher,self.settings()['launch_app'])
            except (subprocess.TimeoutExpired,TimeoutError):
                result={**result,'status':'unknown','message':'실행 창이 열렸는지 확인하지 못했습니다. 터미널을 먼저 확인해 주세요.'};records[signature]=result;self.save(records_path,records);return result
            except (OSError,RuntimeError):
                records[signature]={**result,'status':'failed'};self.save(records_path,records);launcher.unlink(missing_ok=True);raise
            except Exception:
                result={**result,'status':'unknown','message':'실행 창이 열렸는지 확인하지 못했습니다. 터미널을 먼저 확인해 주세요.'};records[signature]=result;self.save(records_path,records);return result
            result={**result,'status':'opened'};records[signature]=result;self.save(records_path,records)
            return result
