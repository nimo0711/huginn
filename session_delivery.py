"""Explicit user-clicked skill requests to already-running local sessions.

Codex uses its local app-server WebSocket API. Claude uses its authenticated
peer inbox; receipt policy remains with the recipient. No terminal keystrokes,
resume processes, permission overrides, or automatic fallback to a new session.
"""
from __future__ import annotations
import atexit
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid


def validate_prompt(prompt):
    if not isinstance(prompt,str) or '\0' in prompt:
        raise ValueError('요청 내용은 텍스트로 입력해 주세요.')
    if len(prompt.encode('utf-16-le'))//2>4000:
        raise ValueError('요청 내용은 4,000자 이내로 입력해 주세요.')
    return prompt if prompt.strip() else ''


def prompt_hash(prompt):
    return hashlib.sha256(prompt.encode()).hexdigest()


def skill_request_text(skill,prompt,current=True):
    prefix='현재 작업의 맥락에서 ' if current else ''
    text=f"작업 보드에서 사용자가 요청했습니다. {prefix}{skill['name']} 스킬 지침을 읽고 실행해 주세요. 스킬 파일: {skill['path']}"
    if prompt:text+='\n\n다음 요청 내용을 스킬의 인자로 사용해 주세요.\n\n'+prompt
    return text


class RPCError(ValueError):
    pass


def private_path(path,kind):
    p=Path(path).resolve(strict=True);info=p.stat()
    check=stat.S_ISSOCK if kind=='socket' else stat.S_ISREG
    if not check(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('현재 사용자 전용 연결 파일이 아닙니다.')
    parent=p.parent.stat()
    if parent.st_uid!=os.getuid() or parent.st_mode & 0o022:
        raise ValueError('연결 폴더 권한을 확인해 주세요.')
    return p


def process_start(pid):
    result=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=2,env={**os.environ,'TZ':'UTC','LC_ALL':'C'})
    return result.stdout.strip() if not result.returncode else ''


def peer_pid(conn):
    # Darwin LOCAL_PEERPID; never substitute the selected metadata's PID.
    if os.uname().sysname=='Darwin':return conn.getsockopt(0,2)
    return None


class LocalRPC:
    """Bounded RFC 6455 client over a private Unix socket, standard library only."""
    def __init__(self,path):self.path=path;self.sock=None;self.buffer=b'';self.sequence=0
    def __enter__(self):
        endpoint=private_path(self.path,'socket')
        self.deadline=time.monotonic()+6
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(6)
        try:
            self.sock.connect(str(endpoint))
            key=base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '+key+'\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            header=b''
            while not header.endswith(b'\r\n\r\n'):
                if len(header)>8192:raise ValueError('연결 응답이 너무 큽니다.')
                header+=self.read(1)
            lines=header.decode('ascii').split('\r\n');fields={k.lower():v.strip() for line in lines[1:] if ':' in line for k,v in [line.split(':',1)]}
            expected=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if lines[0].split()[1]!='101' or fields.get('sec-websocket-accept')!=expected or fields.get('upgrade','').lower()!='websocket':
                raise ValueError('Codex 연결 응답을 확인하지 못했습니다.')
            self.call('initialize',{'clientInfo':{'name':'work_board','version':'1'},'capabilities':{'experimentalApi':True}})
            self.send({'method':'initialized'})
            return self
        except Exception:
            self.sock.close();raise
    def __exit__(self,*_):
        if self.sock:self.sock.close()
    def read(self,n):
        while len(self.buffer)<n:
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('connection deadline')
            self.sock.settimeout(remaining);chunk=self.sock.recv(min(65536,n-len(self.buffer)))
            if not chunk:raise ConnectionError('connection closed')
            self.buffer+=chunk
        data,self.buffer=self.buffer[:n],self.buffer[n:];return data
    def frame(self,payload,opcode=1):
        mask=os.urandom(4);size=len(payload)
        header=bytes([0x80|opcode,0x80|(size if size<126 else 126 if size<65536 else 127)])
        if size>=126:header+=struct.pack('!H' if size<65536 else '!Q',size)
        self.sock.sendall(header+mask+bytes(v^mask[i%4] for i,v in enumerate(payload)))
    def send(self,value):self.frame(json.dumps(value,ensure_ascii=False).encode())
    def receive(self):
        chunks=bytearray();started=False
        while True:
            a,b=self.read(2);opcode=a&15
            if a&0x70 or b&0x80:raise ValueError('지원하지 않는 연결 프레임입니다.')
            size=b&127
            if size==126:size=struct.unpack('!H',self.read(2))[0]
            elif size==127:size=struct.unpack('!Q',self.read(8))[0]
            if size+len(chunks)>8*1024*1024:raise ValueError('연결 응답이 너무 큽니다.')
            payload=self.read(size)
            if opcode>=8:
                if not a&0x80 or size>125:raise ValueError('잘못된 연결 제어 프레임입니다.')
                if opcode==8:raise ConnectionError('connection closed')
                if opcode==9:self.frame(payload,10)
                elif opcode!=10:raise ValueError('알 수 없는 연결 프레임입니다.')
                continue
            if opcode not in (0,1) or (opcode==0)!=started:raise ValueError('잘못된 연결 프레임 순서입니다.')
            started=True;chunks.extend(payload)
            if a&0x80:return json.loads(chunks)
    def call(self,method,params):
        self.deadline=time.monotonic()+6;self.sequence+=1;identifier=self.sequence
        self.send({'id':identifier,'method':method,'params':params})
        while True:
            value=self.receive()
            if value.get('id')!=identifier or 'method' in value:continue
            if 'error' in value:raise RPCError('세션이 실행 요청을 거절했습니다. 해당 세션에서 상태를 확인해 주세요.')
            return value['result']


MESSAGES={
 'pending':'실행 요청을 보내는 중입니다.',
 'accepted':'연결된 세션이 스킬 실행 요청을 접수했습니다.',
 'sent':'스킬 실행을 요청했습니다. 시작·승인 여부는 해당 세션에서 확인해 주세요.',
 'held':'Claude 세션에서 실행 요청 승인이 필요합니다.',
 'delivered':'Claude 세션이 실행 요청을 수락했습니다.',
 'denied':'Claude 세션에서 실행 요청이 거절되었습니다.',
 'expired':'실행 요청이 만료되었습니다. 해당 세션에서 확인해 주세요.',
 'refused':'세션이 실행 요청을 받지 않았습니다. 해당 세션의 연결 설정을 확인해 주세요.',
 'dropped':'세션이 실행 요청을 처리하지 못했습니다. 해당 세션에서 확인해 주세요.',
 'unknown':'실행 요청의 접수 여부를 확인하지 못했습니다. 중복 실행을 피하려면 해당 세션을 먼저 확인해 주세요.'}


class SessionDelivery:
    def __init__(self,state,home=None):
        self.state=Path(state)/'deliveries';self.home=Path(home or Path.home())
        self.lock=threading.RLock();self.pending={};self.listeners={};self.closed=False
        atexit.register(self.close)
    def close(self):
        self.closed=True
        for address,(listener,inode) in list(self.listeners.items()):
            listener.close()
            try:
                if Path(address).lstat().st_ino==inode:Path(address).unlink()
            except OSError:pass
        self.listeners.clear()
    def codex_rpc(self):return LocalRPC(self.home/'.codex/app-server-control/app-server-control.sock')
    def codex_target(self,rpc,session):
        cursor=None
        while True:
            page=rpc.call('thread/loaded/list',{'limit':100,**({'cursor':cursor} if cursor else {})})
            if session['id'] in page['data']:break
            cursor=page.get('nextCursor')
            if not cursor:raise ValueError('실행 중인 Codex 연결을 찾을 수 없습니다.')
        thread=rpc.call('thread/read',{'threadId':session['id'],'includeTurns':False})['thread']
        if (thread.get('id')!=session['id'] or Path(thread.get('cwd','/')).resolve()!=Path(session['cwd_path']).resolve()
                or thread.get('status',{}).get('type') not in ('idle','active') or thread.get('canAcceptDirectInput') is not True):
            raise ValueError('현재 세션에 직접 요청을 전달할 수 없습니다.')
        return thread
    def claude_target(self,session):
        pid=session.get('pid')
        if not isinstance(pid,int) or pid<=0:raise ValueError('실행 중인 Claude 연결을 찾을 수 없습니다.')
        directory=self.home/'.claude/sessions'
        try:meta=json.loads((directory/(str(pid)+'.json')).read_text())
        except (OSError,ValueError):raise ValueError('실행 중인 Claude 연결을 찾을 수 없습니다.')
        if (meta.get('pid')!=pid or meta.get('sessionId')!=session['id'] or meta.get('peerProtocol')!=1
                or Path(meta.get('cwd','/')).resolve()!=Path(session['cwd_path']).resolve()
                or not meta.get('procStart') or process_start(pid)!=meta['procStart']):
            raise ValueError('Claude 세션 연결이 변경되었습니다. 새로 고침해 주세요.')
        address=meta.get('messagingSocketPath','');private_path(address,'socket')
        key=directory/(str(pid)+'.'+hashlib.sha256(address.encode()).hexdigest()+'.key')
        try:auth=json.loads(private_path(key,'file').read_text())
        except (OSError,ValueError):raise ValueError('Claude의 인증된 메시지 연결을 찾을 수 없습니다.')
        if auth.get('procStart')!=meta['procStart'] or not isinstance(auth.get('peerToken'),str) or not auth['peerToken']:
            raise ValueError('Claude 연결 인증 정보가 변경되었습니다.')
        return meta,auth['peerToken']
    def describe(self,session):
        if not session:return {'available':False,'message':'연결할 세션을 선택해 주세요.'}
        try:
            if session['agent']=='claude':self.claude_target(session)
            elif session['agent']=='codex':
                with self.codex_rpc() as rpc:self.codex_target(rpc,session)
            else:raise ValueError('지원하지 않는 세션입니다.')
            return {'available':True,'agent':session['agent'],'message':'현재 세션에서 실행합니다.'}
        except (OSError,ValueError,KeyError,ConnectionError):
            return {'available':False,'message':'실행 중인 세션의 메시지 연결을 확인할 수 없습니다.'}
    def file(self,sid,request_id):
        return self.state/(hashlib.sha256((sid+'\0'+request_id).encode()).hexdigest()+'.json')
    def lookup(self,sid,request_id):
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}',request_id):return None
        try:return json.loads(self.file(sid,request_id).read_text())
        except (OSError,ValueError):return None
    def save(self,row):
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        dest=self.file(row['sid'],row['request_id']);temp=dest.with_suffix('.'+uuid.uuid4().hex)
        with os.fdopen(os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600),'w') as f:json.dump(row,f,ensure_ascii=False)
        temp.replace(dest)
    def update(self,row,status):
        row.update(status=status,message=MESSAGES[status],ok=status not in ('denied','expired','refused','dropped'),updated_at=time.time())
        self.save(row);return dict(row)
    def receive_receipt(self,frame,sender):
        if frame.get('type')!='control' or frame.get('action')!='peer_message_status':return
        with self.lock:
            entry=self.pending.get(frame.get('orig_msg_id'))
            if not entry or entry['target']!=sender:return
            status=frame.get('status')
            if status in ('held','denied','expired','delivered','refused','dropped'):
                row=self.lookup(entry['sid'],entry['request_id'])
                if row:self.update(row,status)
    def reply_socket(self,target):
        directory=Path(target).parent
        address=str(directory/(str(os.getpid())+'.sock'))
        if address in self.listeners:return address
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:listener.bind(address);os.chmod(address,0o600);listener.listen(8);listener.settimeout(1)
        except Exception:listener.close();raise
        self.listeners[address]=(listener,Path(address).lstat().st_ino)
        def receive():
            while not self.closed:
                try:conn,_=listener.accept()
                except socket.timeout:continue
                except OSError:return
                with conn:
                    try:
                        pid=peer_pid(conn);conn.settimeout(2);data=b''
                        while len(data)<65536:
                            part=conn.recv(min(4096,65536-len(data)))
                            if not part:break
                            data+=part
                        for raw in data.splitlines():
                            frame=json.loads(raw);sender=frame.get('from','')
                            entry=self.pending.get(frame.get('orig_msg_id'))
                            if entry and (pid is None or pid==entry['pid']):self.receive_receipt(frame,sender)
                    except (OSError,ValueError,KeyError):pass
        threading.Thread(target=receive,daemon=True,name='skill-receipts').start()
        return address
    def send(self,session,skill,request_id,prompt=""):
        prompt=validate_prompt(prompt);fingerprint=prompt_hash(prompt)
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}',request_id):raise ValueError('실행 요청을 확인할 수 없습니다.')
        if skill['agent']!=session['agent']:raise ValueError('현재 세션과 같은 도구의 스킬을 선택해 주세요.')
        with self.lock:
            previous=self.lookup(session['id'],request_id)
            if previous:
                if previous['skill']!=skill['id'] or previous.get('prompt_hash',prompt_hash(''))!=fingerprint:raise ValueError('스킬이나 요청 내용이 바뀌었습니다. 새 실행 요청을 사용해 주세요.')
                if previous['status']=='pending':return self.update(previous,'unknown')
                return previous
            row={'id':str(uuid.uuid4()),'sid':session['id'],'request_id':request_id,'skill':skill['id'],'label':skill['label'],'prompt_hash':fingerprint}
            text=skill_request_text(skill,prompt)
            if session['agent']=='codex':
                with self.codex_rpc() as rpc:
                    self.codex_target(rpc,session);self.update(row,'pending')
                    try:
                        rpc.call('turn/start',{'threadId':session['id'],'clientUserMessageId':row['id'],'input':[
                            {'type':'text','text':text},{'type':'skill','name':skill['name'],'path':skill['path']}]})
                    except RPCError:return self.update(row,'refused')
                    except (OSError,ValueError,KeyError,ConnectionError):return self.update(row,'unknown')
                    return self.update(row,'accepted')
            if session['agent']!='claude':raise ValueError('지원하지 않는 세션입니다.')
            meta,token=self.claude_target(session);target=meta['messagingSocketPath'];reply=self.reply_socket(target)
            text=re.sub(r'<(?=/?cross-session-message(?:\s|>))',lambda m:'<\\',text)
            frame={'msgV':1,'msg_id':row['id'],'type':'user','session_id':session['id'],'from':'uds:'+reply,'priority':'next',
                   'message':{'role':'user','content':f'<cross-session-message from="uds:{reply}" from-name="작업 보드" from-mode="prompting">\n{text}\n</cross-session-message>'}}
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
                conn.settimeout(3);conn.connect(target)
                if peer_pid(conn) not in (None,meta['pid']):raise ValueError('Claude 연결 대상이 변경되었습니다.')
                conn.sendall((json.dumps({'type':'auth','token':token})+'\n').encode())
                self.pending[row['id']]={'sid':session['id'],'request_id':request_id,'target':'uds:'+target,'pid':meta['pid']}
                self.update(row,'pending')
                try:
                    conn.sendall((json.dumps(frame,ensure_ascii=False)+'\n').encode());conn.shutdown(socket.SHUT_WR)
                    while conn.recv(4096):pass
                except OSError:return self.update(row,'unknown')
                return self.update(row,'sent')
