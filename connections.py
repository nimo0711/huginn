"""One configuration contract for the settings screen, CLI and data readers."""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit, quote


SCHEMA = {
    'jira': {'label':'Jira','description':'작업 제목·스토리·상태·스프린트', 'defaults':{'enabled':False,'site':'','project':''},
             'fields':[{'key':'site','label':'사이트 주소','type':'url','hint':'현재 acli에 로그인한 사이트의 주소'},
                       {'key':'project','label':'프로젝트 키','type':'text','hint':'스프린트에서 조회할 프로젝트 키'}],
             'help':'주소와 프로젝트를 저장한 뒤 로그인 버튼을 누르세요. 터미널과 브라우저에서 본인 계정으로 로그인하고, 보드로 돌아와 연결 확인을 누릅니다. 이미 로그인했다면 연결 확인부터 사용할 수 있습니다. 인증은 이 Mac의 acli가 관리합니다.'},
    'gitlab': {'label':'GitLab','description':'그룹의 MR·머지 상태', 'defaults':{'enabled':False,'host':'','group':'','cwd':''},
               'fields':[{'key':'host','label':'호스트','type':'text','hint':'프로토콜을 제외한 호스트와 필요한 포트'},
                         {'key':'group','label':'그룹 경로','type':'text','hint':'하위 그룹은 /로 구분'},
                         {'key':'cwd','label':'작업 폴더','type':'path','hint':'GitLab CLI가 사용할 기존 로컬 폴더'}],
               'help':'호스트·그룹·작업 폴더를 저장한 뒤 로그인 버튼을 누르세요. 터미널의 GitLab 로그인 안내를 마치고 연결 확인을 누릅니다. 이미 로그인했다면 연결 확인부터 사용할 수 있습니다. 인증은 이 Mac의 glab이 관리합니다.'},
    'git': {'label':'로컬 Git','description':'등록한 폴더의 저장소·커밋 기록', 'defaults':{'enabled':False,'roots':[]},
            'fields':[{'key':'roots','label':'조회할 폴더','type':'paths','hint':'한 줄에 한 폴더. 폴더 자체와 두 단계 아래의 저장소를 읽습니다.'}],
            'help':'기존 로컬 폴더를 등록합니다. clone·fetch·checkout은 실행하지 않습니다.'},
    'claude': {'label':'Claude','description':'로컬 세션·작업 기록 연결', 'defaults':{'enabled':True},'fields':[],
               'help':'현재 사용자의 ~/.claude 세션을 읽습니다. 연결을 중지해도 실행 중인 Claude는 종료하지 않습니다.'},
    'codex': {'label':'Codex','description':'로컬 세션·작업 기록 연결', 'defaults':{'enabled':True},'fields':[],
              'help':'현재 사용자의 ~/.codex 세션을 읽습니다. 연결을 중지해도 실행 중인 Codex는 종료하지 않습니다.'},
    'summary': {'label':'AI 제목 요약','description':'요청 내용을 짧은 작업 제목으로 요약', 'defaults':{'enabled':True,'model':'haiku'},
                'fields':[{'key':'model','label':'Claude 모델','type':'text','hint':'설치된 Claude CLI에서 사용할 수 있는 모델 이름'}],
                'help':'Claude CLI로 제목을 생성합니다. 연결 확인은 CLI 설치만 확인하며 요약을 실행하거나 사용량을 발생시키지 않습니다.'},
}


def cli(name):
    return next((str(p) for p in [Path.home()/'.local/bin'/name,Path('/opt/homebrew/bin')/name,
                  Path('/usr/local/bin')/name] if p.is_file() and os.access(p,os.X_OK)),shutil.which(name) or name)


def run(args, cwd=None, env=None):
    try:
        p=subprocess.run(args,cwd=cwd or None,env=env,capture_output=True,text=True,timeout=15)
        return p.returncode,p.stdout
    except (OSError,subprocess.SubprocessError):return -1,''


def gitlab_api(endpoint,cfg):
    # glab 1.89 rejects a port in --hostname. Its documented GITLAB_HOST
    # override supports these hosts without changing the user's global config.
    env={**os.environ,'GITLAB_HOST':cfg['host']}
    return run([cli('glab'),'api',endpoint],cfg['cwd'],env=env)


def jira_account_matches(site):
    if not site:return True
    code,out=run([cli('acli'),'jira','auth','status'])
    hosts=set(re.findall(r'(?:https?://)?([A-Za-z0-9.-]+\.atlassian\.net)',out))
    ok=code==0 and urlsplit(site).hostname in hosts
    return ok


def probe(args,cwd=None,env=None):
    try:
        p=subprocess.run(args,cwd=cwd or None,env=env,capture_output=True,text=True,timeout=15)
    except FileNotFoundError:return {'status':'cli_missing'}
    except subprocess.TimeoutExpired:return {'status':'timeout'}
    except (OSError,subprocess.SubprocessError):return {'status':'check_failed'}
    if p.returncode==0:return {'status':'ok','out':p.stdout}
    error=(p.stderr+'\n'+p.stdout).lower()
    if any(x in error for x in ('401','not logged','not authenticated','authentication failed','no token','not configured','log in to','login required')):status='login_required'
    elif any(x in error for x in ('403','forbidden','permission denied')):status='permission_denied'
    elif any(x in error for x in ('404','not found','does not exist')):status='not_found_or_denied'
    elif any(x in error for x in ('timeout','timed out','deadline exceeded')):status='timeout'
    elif any(x in error for x in ('dial tcp','no such host','connection refused','connection reset','network','tls','certificate','resolve host','503','502')):status='network_error'
    else:status='check_failed'
    return {'status':status}


def values_hash(values):
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


class Connections:
    def __init__(self,state,home=None):
        self.state=Path(state);self.home=Path(home or Path.home());self.path=self.state/'connections.json'
        self.check_path=self.state/'connection-checks.json'

    def read(self):
        try:
            data=json.loads(self.path.read_text())
            if not isinstance(data,dict):raise ValueError()
            return data
        except FileNotFoundError:return {}
        except (ValueError,OSError):
            # Corrupt configuration must not silently enable a disabled source.
            return {kind:{'enabled':False,'registered':False} for kind in SCHEMA}

    def get(self,kind):
        if kind not in SCHEMA:raise ValueError('지원하지 않는 연결 종류입니다.')
        values=copy.deepcopy(SCHEMA[kind]['defaults']);saved=self.read().get(kind,{})
        if isinstance(saved,dict):
            values.update({k:v for k,v in saved.items() if k in values})
        return values

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.read(),sort_keys=True).encode()).hexdigest()

    def catalog(self):
        data=self.read();rows=[]
        try:checks=json.loads(self.check_path.read_text())
        except (OSError,ValueError):checks={}
        if not isinstance(checks,dict):checks={}
        for kind,spec in SCHEMA.items():
            values=self.get(kind);fingerprint=values_hash(values);check=checks.get(kind)
            if not isinstance(check,dict) or check.get('fingerprint')!=fingerprint:check=None
            rows.append({'kind':kind,'label':spec['label'],'description':spec['description'],
                         'help':spec['help'],'fields':spec['fields'],'values':values,'fingerprint':fingerprint,'check':check,
                         'registered':data.get(kind,{}).get('registered',kind in data or kind in ('claude','codex','summary'))})
        return rows

    def _write(self,kind,update):
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (self.state/'.lock').open('a') as lock:
            os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
            data=self.read();value=update(data);data[kind]=value
            tmp=self.state/('.connections.'+uuid.uuid4().hex)
            try:
                with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(data,f,ensure_ascii=False)
                tmp.replace(self.path)
            finally:tmp.unlink(missing_ok=True)
        return self.get(kind)

    def save(self,kind,changes,expected=None):
        if kind not in SCHEMA or not isinstance(changes,dict):raise ValueError('연결 종류와 설정 값을 확인해 주세요.')
        defaults=SCHEMA[kind]['defaults']
        if expected is not None and (not isinstance(expected,dict) or set(expected)!=set(changes)):
            raise ValueError('수정 전 설정 값을 확인할 수 없습니다. 다시 열어 주세요.')
        if set(changes)-set(defaults):raise ValueError('지원하지 않는 설정 항목입니다. 인증 토큰은 저장하지 않습니다.')
        normalized={}
        for key,value in changes.items():
            if type(value) is not type(defaults[key]):raise ValueError('설정 값의 형식이 올바르지 않습니다.')
            if isinstance(value,str):
                value=value.strip()
                if len(value)>2048 or any(ord(c)<32 for c in value):raise ValueError('설정 값에 사용할 수 없는 문자가 있습니다.')
            if key=='site' and value:
                u=urlsplit(value)
                if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
                    raise ValueError('사이트는 인증 정보 없는 HTTPS 주소로 입력해 주세요.')
                value=value.rstrip('/')
            if key=='project' and value and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,39}',value):raise ValueError('프로젝트 키를 확인해 주세요.')
            if key=='host' and value and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?',value):raise ValueError('호스트와 포트만 입력해 주세요.')
            if key=='group' and value and not re.fullmatch(r'[\w.-]+(?:/[\w.-]+)*',value):raise ValueError('그룹 경로를 확인해 주세요.')
            if key=='model' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,119}',value):raise ValueError('모델 이름을 확인해 주세요.')
            if key=='cwd' and value:
                value=str(Path(value).expanduser().resolve())
                if not Path(value).is_dir():raise ValueError('작업 폴더가 없습니다.')
            if key=='roots':
                if len(value)>20 or any(not isinstance(p,str) or not p.strip() or '\x00' in p for p in value):raise ValueError('조회할 폴더는 최대 20개입니다.')
                value=list(dict.fromkeys(str(Path(p).expanduser().resolve()) for p in value))
                if any(not Path(p).is_dir() for p in value):raise ValueError('조회할 폴더가 없습니다.')
            normalized[key]=value
        def update(data):
            current={**defaults,**{k:v for k,v in data.get(kind,{}).items() if k in defaults}}
            if expected is not None and any(current[k]!=v for k,v in expected.items()):
                raise ValueError('다른 작업에서 같은 설정을 변경했습니다. 입력 내용을 확인한 뒤 설정을 다시 열어 주세요.')
            values={**current,**normalized}
            if values['enabled']:
                required={'jira':('site','project'),'gitlab':('host','group','cwd'),'git':('roots',)}.get(kind,())
                if any(not values[k] for k in required):raise ValueError('연결에 필요한 설정을 모두 입력해 주세요.')
            return {**values,'registered':True}
        return self._write(kind,update)

    def remove(self,kind):
        if kind not in SCHEMA:raise ValueError('지원하지 않는 연결 종류입니다.')
        return self._write(kind,lambda data:{**SCHEMA[kind]['defaults'],'enabled':False,'registered':False})

    def check(self,kind):
        values=self.get(kind);fingerprint=values_hash(values);ok=False;status='check_failed';message='연결을 확인하지 못했습니다.'
        if not values['enabled']:
            return self._save_check(kind,values,{'ok':False,'status':'disabled','message':'보드에서 사용이 꺼져 있습니다. 설정에서 켠 뒤 확인해 주세요.'})
        if kind=='jira':
            if not values['site'] or not values['project']:status='not_configured'
            else:
                auth=probe([cli('acli'),'jira','auth','status']);status=auth['status']
                if status=='ok':
                    hosts=set(re.findall(r'([A-Za-z0-9.-]+\.atlassian\.net)',auth.get('out','')))
                    status='ok' if urlsplit(values['site']).hostname in hosts else 'site_mismatch'
                if status=='ok':
                    project=probe([cli('acli'),'jira','project','view','--key',values['project'],'--json']);status=project['status']
                    if status=='ok':
                        try:ok=json.loads(project['out']).get('key')==values['project']
                        except (ValueError,AttributeError):pass
                        if ok:
                            auth=probe([cli('acli'),'jira','auth','status'])
                            ok=auth['status']=='ok' and urlsplit(values['site']).hostname in set(re.findall(r'([A-Za-z0-9.-]+\.atlassian\.net)',auth.get('out','')))
                            status='connected' if ok else 'site_mismatch' if auth['status']=='ok' else auth['status']
                        else:status='check_failed'
            message=values['project']+' 프로젝트 조회를 확인했습니다.'
        elif kind=='gitlab':
            if values['host'] and values['group'] and Path(values['cwd']).is_dir():
                env={**os.environ,'GITLAB_HOST':values['host']}
                for endpoint in ('user','groups/'+quote(values['group'],safe='')):
                    result=probe([cli('glab'),'api',endpoint],values['cwd'],env);status=result['status']
                    if status!='ok':break
                    try:ok=isinstance(json.loads(result['out']).get('id'),int)
                    except (ValueError,AttributeError):ok=False
                    if not ok:status='check_failed';break
                ok=ok and status=='ok'
                if ok:status='connected'
            else:status='not_configured'
            message=values['group']+' 그룹 조회를 확인했습니다.'
        elif kind=='git':
            ok=bool(values['roots']) and all(Path(p).is_dir() for p in values['roots'])
            message='조회할 폴더를 확인했습니다.' if ok else '기존 로컬 폴더를 등록해 주세요.'
        elif kind in ('claude','codex'):
            source=self.home/('.'+kind)/('projects' if kind=='claude' else 'state_5.sqlite')
            ok=source.exists();message='로컬 세션 저장소를 확인했습니다.' if ok else '로컬 세션 저장소를 찾지 못했습니다. 해당 도구를 먼저 실행해 주세요.'
        elif kind=='summary':
            code,_=run([cli('claude'),'--version']);ok=code==0
            message='Claude CLI를 확인했습니다. 로그인·모델 실행은 점검하지 않았습니다.' if ok else 'Claude CLI 설치를 확인해 주세요.'
        if kind in ('jira','gitlab') and not ok:
            message={
                'cli_missing':('Atlassian CLI' if kind=='jira' else 'GitLab CLI')+'가 설치되어 있지 않습니다. 설치 안내를 열어 설치한 뒤 다시 확인해 주세요.',
                'login_required':'로그인이 필요하거나 인증이 만료되었습니다. 로그인한 뒤 다시 확인해 주세요.',
                'site_mismatch':'로그인한 Jira 사이트가 저장한 주소와 다릅니다. 사이트 전환을 누른 뒤 다시 확인해 주세요.',
                'permission_denied':'로그인 계정에 조회 권한이 없습니다. 프로젝트·그룹 관리자에게 접근 권한을 요청해 주세요.',
                'not_found_or_denied':'대상을 찾을 수 없거나 조회 권한이 없습니다. 프로젝트 키·그룹 경로와 계정 권한을 확인해 주세요.',
                'network_error':'서버에 연결하지 못했습니다. 주소·VPN·네트워크와 인증서 설정을 확인한 뒤 다시 시도해 주세요.',
                'timeout':'서버 응답 시간이 초과됐습니다. 네트워크를 확인한 뒤 다시 시도해 주세요.',
                'not_configured':'연결 설정을 먼저 저장해 주세요.',
                'check_failed':'연결 확인에 실패했습니다. 로그인과 대상 설정을 확인한 뒤 다시 시도해 주세요.'
            }.get(status,'연결을 확인하지 못했습니다.')
        elif kind not in ('jira','gitlab'):status='available' if ok else 'unavailable'
        return self._save_check(kind,values,{'ok':ok,'status':status,'message':message})

    def _save_check(self,kind,values,result):
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (self.state/'.lock').open('a') as lock:
            os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
            if self.get(kind)!=values:
                return {'ok':False,'status':'config_changed','message':'확인 중 설정이 변경됐습니다. 다시 확인해 주세요.','checked_at':time.time()}
            result={**result,'checked_at':time.time(),'fingerprint':values_hash(values)}
            try:checks=json.loads(self.check_path.read_text())
            except (OSError,ValueError):checks={}
            if not isinstance(checks,dict):checks={}
            checks[kind]=result;tmp=self.state/('.checks.'+uuid.uuid4().hex)
            try:
                with os.fdopen(os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:json.dump(checks,f,ensure_ascii=False)
                tmp.replace(self.check_path)
            finally:tmp.unlink(missing_ok=True)
        return result

    def login_command(self,kind,switch=False):
        values=self.get(kind)
        if kind=='jira' and values['site']:
            host=urlsplit(values['site']).hostname
            args=[cli('acli'),'jira','auth','switch','--site',host] if switch else [cli('acli'),'jira','auth','login','--web','--site',host]
            return args,str(self.home),{},'Jira 로그인 대상: '+host
        if kind=='gitlab' and values['host'] and values['cwd']:
            return [cli('glab'),'auth','login','--hostname',values['host'],'--use-keyring'],values['cwd'],{},'GitLab 로그인 대상: '+values['host']
        raise ValueError('로그인할 연결의 주소와 작업 폴더를 먼저 저장해 주세요.')

    def guide(self,wb):
        lines=['연결 설정은 화면과 아래 CLI가 같은 저장소를 사용합니다. 종류별로 한 연결을 설정하며, 로컬 Git은 여러 폴더를 등록할 수 있습니다.',
               f'{wb} actions connections list',f'{wb} actions connections save --kind <종류> --file <JSON 파일>',
               f'{wb} actions connections check --kind <종류>',f'{wb} actions connections remove --kind <종류>',
               'JSON에는 변경할 필드만 넣으세요. enabled는 true/false입니다. 등록 후 list로 다시 읽고 check 결과를 확인하세요. 저장은 인증이 아닙니다. 설정 화면의 로그인 버튼을 사용자가 누르고 인증을 마친 뒤 연결 확인을 실행합니다. 비밀번호·토큰을 JSON이나 도움말에 넣지 마세요.',
               '수동 Jira 로그인: acli jira auth login --web. 다른 사이트라면 acli jira auth switch --site <저장한 사이트 호스트>.',
               '수동 GitLab 로그인: glab auth login --hostname "<저장한 호스트>" --use-keyring. 필요한 토큰은 터미널의 인증 입력에서만 받습니다.',
               'check의 connected는 프로젝트·그룹 조회 성공, available은 로컬 저장소·CLI 확인입니다. login_required·site_mismatch·permission_denied·network_error·cli_missing은 안내에 따라 복구하고 다시 확인합니다.']
        for kind,spec in SCHEMA.items():
            example={**spec['defaults'],'enabled':True}
            lines.append(kind+' · '+spec['label']+': '+json.dumps(example,ensure_ascii=False)+'\n'+spec['help'])
        lines.append('연결 제거는 보드의 연결만 중지합니다. 계정 로그아웃·서버 종료·저장소 삭제는 하지 않습니다. 현재 작업의 기존 설정을 먼저 조회하고 사용자가 요청한 종류와 필드만 변경하세요.')
        return '\n'.join(lines)


STORE=Connections(Path(__file__).resolve().parent/'.actions')
