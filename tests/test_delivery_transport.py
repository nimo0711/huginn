import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
import sys
from unittest.mock import patch
import session_delivery as d

@unittest.skipIf(sys.platform == 'win32', 'requires Unix-socket owner and mode validation')
class TransportTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def listener(self,name):
  p=self.root/name;s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.bind(str(p));p.chmod(0o600);s.listen(1);s.settimeout(5)
  self.addCleanup(s.close);return p,s
 def test_real_websocket_framing_ping_fragmentation_and_rpc_rejection(self):
  p,listener=self.listener('codex.sock');errors=[];methods=[]
  def server():
   try:
    conn,_=listener.accept()
    with conn:
     conn.settimeout(5);header=b''
     while not header.endswith(b'\r\n\r\n'):header+=conn.recv(1)
     key=next(x.split(b': ',1)[1] for x in header.split(b'\r\n') if x.startswith(b'Sec-WebSocket-Key:'))
     accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
     conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
     def read(n):
      data=b''
      while len(data)<n:
       chunk=conn.recv(n-len(data))
       if not chunk:raise EOFError()
       data+=chunk
      return data
     def frame(payload,opcode=1,final=True):
      n=len(payload);head=bytes([(0x80 if final else 0)|opcode,n if n<126 else 126])
      if n>=126:head+=struct.pack('!H',n)
      conn.sendall(head+payload)
     while True:
      try:a,b=read(2)
      except EOFError:break
      assert b&128
      size=b&127
      if size==126:size=struct.unpack('!H',read(2))[0]
      elif size==127:size=struct.unpack('!Q',read(8))[0]
      mask=read(4);payload=read(size);payload=bytes(v^mask[i%4] for i,v in enumerate(payload))
      if a&15==10:continue
      row=json.loads(payload);methods.append(row['method'])
      if 'id' not in row:continue
      response={'id':row['id'],'result':{}} if row['method']=='initialize' else {'id':row['id'],'error':{'code':-1,'message':'no'}}
      raw=json.dumps(response).encode();frame(raw[:10],final=False);frame(b'hi',opcode=9);frame(raw[10:],opcode=0)
   except Exception as e:errors.append(repr(e))
  t=threading.Thread(target=server);t.start()
  with d.LocalRPC(p) as rpc:
   with self.assertRaises(d.RPCError):rpc.call('turn/start',{'input':[{'text':'한글'*100}]})
  t.join(5);self.assertFalse(t.is_alive());self.assertEqual(errors,[]);self.assertEqual(methods,['initialize','initialized','turn/start'])
 def test_claude_authenticated_frame_and_held_then_delivered_receipts(self):
  target,listener=self.listener('claude.sock');pid=os.getpid();directory=self.root/'.claude/sessions';directory.mkdir(parents=True)
  meta={'pid':pid,'sessionId':'claude-session','cwd':str(self.root),'procStart':'start','peerProtocol':1,'messagingSocketPath':str(target)}
  (directory/(str(pid)+'.json')).write_text(json.dumps(meta))
  key=directory/(str(pid)+'.'+hashlib.sha256(str(target).encode()).hexdigest()+'.key');key.write_text(json.dumps({'peerToken':'fixture-secret','procStart':'start'}));key.chmod(0o600)
  link=d.SessionDelivery(self.root/'state',self.root);self.addCleanup(link.close);frames=[];errors=[]
  def server():
   try:
    conn,_=listener.accept()
    with conn:
     f=conn.makefile('rb');frames.extend(json.loads(f.readline()) for _ in range(2));f.close()
    frame=frames[1]
    for status in ['held','delivered']:
     with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as receipt:
      receipt.connect(frame['from'][4:]);receipt.sendall((json.dumps({'type':'control','action':'peer_message_status','orig_msg_id':frame['msg_id'],'from':'uds:'+str(target),'status':status})+'\n').encode());receipt.shutdown(socket.SHUT_WR)
      while receipt.recv(4096):pass
     time.sleep(.05)
   except Exception as e:errors.append(repr(e))
  t=threading.Thread(target=server);t.start()
  session={'agent':'claude','id':'claude-session','pid':pid,'cwd_path':str(self.root)}
  skill={'id':'claude:review','name':'review','path':str(self.root/'SKILL.md'),'label':'검수','agent':'claude'}
  with patch.object(d,'process_start',return_value='start'):
   row=link.send(session,skill,'click-once','한글 요청\n두 번째 줄');again=link.send(session,skill,'click-once','한글 요청\n두 번째 줄')
  t.join(5)
  deadline=time.monotonic()+2
  while time.monotonic()<deadline and link.lookup(session['id'],'click-once')['status']!='delivered':time.sleep(.02)
  self.assertEqual(errors,[]);self.assertEqual(frames[0],{'type':'auth','token':'fixture-secret'})
  self.assertEqual(frames[1]['session_id'],session['id']);self.assertEqual(frames[1]['priority'],'next')
  self.assertIn('한글 요청\n두 번째 줄',frames[1]['message']['content']);self.assertIn('from-mode="prompting"',frames[1]['message']['content']);self.assertEqual(row['id'],again['id'])
  self.assertEqual(link.lookup(session['id'],'click-once')['status'],'delivered')
  self.assertNotIn('fixture-secret',next((self.root/'state/deliveries').glob('*.json')).read_text())

if __name__=='__main__':unittest.main()
