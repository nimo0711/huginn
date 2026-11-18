import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch
import session_delivery as delivery

class FakeRPC:
 def __init__(self,thread=None,error=None):
  self.thread=thread or {'id':'thread-1','cwd':'/project','status':{'type':'active'},'canAcceptDirectInput':True}
  self.error=error;self.calls=[]
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def call(self,method,params):
  self.calls.append((method,params))
  if method=='thread/loaded/list':return {'data':['thread-1'],'nextCursor':None}
  if method=='thread/read':return {'thread':self.thread}
  if self.error:raise self.error
  return {'turn':{'id':'turn-1','status':'inProgress'}}

@unittest.skipIf(sys.platform == 'win32', 'requires Unix-socket direct session delivery support')
class DeliveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.link=delivery.SessionDelivery(self.root/'state',self.root);self.addCleanup(self.link.close)
  self.session={'id':'thread-1','agent':'codex','cwd_path':'/project'}
  self.skill={'id':'codex:review','name':'review','path':'/project/.agents/skills/review/SKILL.md','agent':'codex','label':'검수'}
 def send(self,rpc,nonce='test-request'):
  with patch.object(self.link,'codex_rpc',return_value=rpc):return self.link.send(self.session,self.skill,nonce)
 def test_existing_codex_turn_inherits_config_and_is_idempotent(self):
  rpc=FakeRPC();a=self.send(rpc);b=self.send(rpc)
  self.assertEqual(a,b);self.assertEqual(a['status'],'accepted')
  calls=[p for method,p in rpc.calls if method=='turn/start'];self.assertEqual(len(calls),1)
  self.assertEqual(set(calls[0]),{'threadId','clientUserMessageId','input'})
  self.assertEqual(calls[0]['threadId'],'thread-1');self.assertEqual(calls[0]['input'][1],{'type':'skill','name':'review','path':self.skill['path']})
 def test_stale_folder_or_noninteractive_target_never_receives(self):
  for change in [{'cwd':'/other'},{'canAcceptDirectInput':False},{'status':{'type':'notLoaded'}},{'id':'other'}]:
   rpc=FakeRPC();rpc.thread.update(change)
   with self.assertRaises(ValueError):self.send(rpc)
   self.assertNotIn('turn/start',[m for m,p in rpc.calls])
 def test_agent_mismatch_cannot_silently_open_other_cli(self):
  self.skill['agent']='claude'
  with self.assertRaises(ValueError):self.send(FakeRPC())
 def test_transport_timeout_after_write_is_unknown_and_never_automatically_resent(self):
  rpc=FakeRPC(error=TimeoutError())
  result=self.send(rpc);self.assertEqual(result['status'],'unknown')
  other=delivery.SessionDelivery(self.root/'state',self.root)
  with patch.object(other,'codex_rpc',side_effect=AssertionError('must not resend')):
   self.assertEqual(other.send(self.session,self.skill,'test-request'),result)
 def test_rpc_rejection_is_not_success(self):
  result=self.send(FakeRPC(error=delivery.RPCError('rejected')))
  self.assertEqual(result['status'],'refused');self.assertFalse(result['ok'])
 def test_request_identity_cannot_be_reused_for_different_skill(self):
  self.send(FakeRPC());self.skill['id']='codex:other'
  with self.assertRaises(ValueError):self.send(FakeRPC())
 def test_unknown_session_is_not_resumed_or_created(self):
  rpc=FakeRPC();rpc.thread['status']={'type':'idle'}
  original=rpc.call
  rpc.call=lambda m,p: {'data':[],'nextCursor':None} if m=='thread/loaded/list' else original(m,p)
  with self.assertRaises(ValueError):self.send(rpc)
  self.assertEqual([m for m,p in rpc.calls],[])
 def test_claude_metadata_rejects_pid_reuse_and_cwd_mismatch(self):
  folder=self.root/'.claude/sessions';folder.mkdir(parents=True)
  meta={'pid':123,'sessionId':'sid','cwd':'/project','procStart':'original','peerProtocol':1,'messagingSocketPath':'/tmp/example.sock'}
  (folder/'123.json').write_text(json.dumps(meta))
  session={'agent':'claude','id':'sid','pid':123,'cwd_path':'/project'}
  with patch.object(delivery,'process_start',return_value='changed'):
   with self.assertRaises(ValueError):self.link.claude_target(session)
  session['cwd_path']='/other'
  with patch.object(delivery,'process_start',return_value='original'):
   with self.assertRaises(ValueError):self.link.claude_target(session)
 def test_receipt_only_changes_its_known_request_and_correct_sender(self):
  result=self.send(FakeRPC(error=TimeoutError()))
  entry=self.link.lookup(self.session['id'],'test-request')
  self.link.receive_receipt({'type':'control','action':'peer_message_status','orig_msg_id':entry['id'],'status':'held'},'wrong')
  self.assertEqual(self.link.lookup(self.session['id'],'test-request')['status'],'unknown')

 def test_codex_receives_multiline_user_request_with_selected_skill(self):
  rpc=FakeRPC();prompt='이번 변경을 검토해 주세요.\n한글과 "인용", `코드`를 그대로 유지해 주세요.'
  with patch.object(self.link,'codex_rpc',return_value=rpc):
   self.link.send(self.session,self.skill,'prompt-request',prompt)
   self.link.send(self.session,self.skill,'prompt-request',prompt)
  calls=[p for method,p in rpc.calls if method=='turn/start'];self.assertEqual(len(calls),1)
  self.assertTrue(calls[0]['input'][0]['text'].endswith(prompt));self.assertEqual(calls[0]['input'][1]['path'],self.skill['path'])
 def test_same_request_id_cannot_drop_changed_prompt(self):
  rpc=FakeRPC()
  with patch.object(self.link,'codex_rpc',return_value=rpc):
   self.link.send(self.session,self.skill,'prompt-request','첫 번째 요청')
   with self.assertRaises(ValueError):self.link.send(self.session,self.skill,'prompt-request','다른 요청')
 def test_invalid_prompt_fails_before_touching_session(self):
  for prompt in [None,{},'a'*4001,'nul\0value']:
   with patch.object(self.link,'codex_rpc',side_effect=AssertionError('must validate before connecting')):
    with self.assertRaises(ValueError):self.link.send(self.session,self.skill,'prompt-request',prompt)

if __name__=='__main__':unittest.main()
