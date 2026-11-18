from io import BytesIO
from email.message import Message
from unittest.mock import patch
import json
import unittest
import server

class ApiTests(unittest.TestCase):
 def handler(self,body,origin='http://127.0.0.1:8790',token=None):
  h=object.__new__(server.Handler);h.headers=Message();h.headers['Origin']=origin;h.headers['Host']='127.0.0.1:8790';h.headers['Content-Type']='application/json';h.headers['X-Work-Board-Token']=token or server.ACTION_TOKEN
  raw=json.dumps(body).encode();h.headers['Content-Length']=str(len(raw));h.rfile=BytesIO(raw);h.result=None
  h._json=lambda code,data:setattr(h,'result',(code,data))
  return h
 def test_cross_origin_and_missing_token_cannot_launch(self):
  for origin,token in [('http://evil.test','ok'),('null','ok'),('http://127.0.0.1:8790','bad')]:
   h=self.handler({'action':'launch','sid':'s'},origin,token)
   with patch.object(server.ACTIONS,'launch') as launch:h._action();launch.assert_not_called()
   self.assertEqual(h.result[0],403)
 def test_unknown_session_never_reaches_launch(self):
  h=self.handler({'action':'launch','sid':'unknown'})
  with patch.object(server.scan,'all_sessions',return_value=[]),patch.object(server.ACTIONS,'launch') as launch:
   h._action();launch.assert_not_called()
  self.assertEqual(h.result[0],400)
 def test_browser_cannot_supply_cwd_or_shell_command(self):
  h=self.handler({'action':'launch','sid':'s','skill':'id','request_id':'valid-click','cwd':'/tmp/evil','command':'touch evil'})
  with patch.object(server.scan,'all_sessions',return_value=[{'id':'s','cwd_path':'/known'}]),patch.object(server.ACTIONS,'launch',return_value={'id':'job','status':'opened'}) as launch:
   h._action()
  self.assertEqual(launch.call_args.args,('id','/known','valid-click',''))
 def test_open_error_returns_failure(self):
  h=self.handler({'action':'launch','sid':'s','skill':'id','request_id':'valid-click'})
  with patch.object(server.scan,'all_sessions',return_value=[{'id':'s','cwd_path':'/known'}]),patch.object(server.ACTIONS,'launch',side_effect=RuntimeError('열기 실패')):h._action()
  self.assertEqual(h.result[0],409)
 def test_document_html_is_sandboxed_away_from_action_origin(self):
  h=object.__new__(server.Handler);h.path='/doc/test.html';h.wfile=BytesIO();headers={}
  h.send_response=lambda code:None;h.send_header=lambda k,v:headers.update({k:v});h.end_headers=lambda:None
  h._send(200,b'<script>fetch("/api/actions")</script>','text/html')
  self.assertIn('sandbox',headers.get('Content-Security-Policy',''))
  self.assertNotIn('allow-same-origin',headers['Content-Security-Policy'])
 def test_action_catalog_rejects_foreign_host_before_reading_sessions(self):
  h=self.handler({});h.path='/api/actions';h.headers.replace_header('Host','evil.test')
  with patch.object(server.scan,'all_sessions') as scan:h.do_GET();scan.assert_not_called()
  self.assertEqual(h.result[0],403)
 def test_board_cannot_be_embedded_for_clickjacking(self):
  h=object.__new__(server.Handler);h.path='/';h.wfile=BytesIO();headers={}
  h.send_response=lambda code:None;h.send_header=lambda k,v:headers.update({k:v});h.end_headers=lambda:None
  h._send(200,b'board','text/html')
  self.assertEqual(headers.get('X-Frame-Options'),'DENY')
  self.assertIn("frame-ancestors 'none'",headers.get('Content-Security-Policy',''))

 def test_delivery_rechecks_session_and_ignores_browser_target_details(self):
  selected={'id':'s','cwd_path':'/known','agent':'codex'}
  h=self.handler({'action':'deliver','sid':'s','skill':'id','request_id':'valid-click','cwd':'/evil','pid':123,'socket':'/evil.sock'})
  with patch.object(server.scan,'all_sessions',return_value=[selected]),patch.object(server.ACTIONS,'deliver',return_value={'ok':True,'status':'accepted'}) as send:
   h._action()
  self.assertEqual(send.call_args.args,('id',selected,'valid-click',''));self.assertEqual(h.result[0],200)
 def test_cross_origin_and_missing_session_cannot_deliver(self):
  for origin,token,sessions in [('http://evil.test',None,[]),('http://127.0.0.1:8790','wrong',[]),('http://127.0.0.1:8790',None,[])]:
   h=self.handler({'action':'deliver','sid':'missing'},origin,token)
   with patch.object(server.scan,'all_sessions',return_value=sessions),patch.object(server.ACTIONS,'deliver') as send:
    h._action();send.assert_not_called()
   self.assertIn(h.result[0],[400,403])

 def test_delivery_connection_error_does_not_tell_user_to_open_a_new_terminal(self):
  h=self.handler({'action':'deliver','sid':'s','skill':'id','request_id':'valid-click'})
  with patch.object(server.scan,'all_sessions',return_value=[{'id':'s','cwd_path':'/known','agent':'codex'}]),patch.object(server.ACTIONS,'deliver',side_effect=ConnectionError()):h._action()
  self.assertEqual(h.result[0],409);self.assertIn('세션 연결',h.result[1]['error']);self.assertNotIn('CLI',h.result[1]['error'])
 def test_http_keeps_long_korean_prompt_through_skill_execution(self):
  import tempfile
  from pathlib import Path
  import board_actions
  from test_session_delivery import FakeRPC
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);skill=root/'.codex/skills/review/SKILL.md';skill.parent.mkdir(parents=True);skill.write_text('---\nname: review\ndescription: Review changes\n---\nInstructions')
   store=board_actions.ActionStore(root/'state',root);self.addCleanup(store.delivery.close)
   item=store.catalog(root)[0];session={'id':'thread-1','agent':'codex','cwd_path':str(root)};prompt='한글\n'*1300
   rpc=FakeRPC();rpc.thread['cwd']=str(root)
   h=self.handler({'action':'deliver','sid':'thread-1','skill':item['id'],'request_id':'prompt-request','prompt':prompt})
   # FakeRPC supplies a supported direct connection on every runner.
   with patch.object(server,'ACTIONS',store), patch.object(server.scan,'all_sessions',return_value=[session]), \
        patch('session_delivery.IS_WINDOWS', False), patch.object(store.delivery,'codex_rpc',return_value=rpc):
    h._action()
   self.assertEqual(h.result[0],200);self.assertEqual(h.result[1]['status'],'accepted')
   sent=next(p for method,p in rpc.calls if method=='turn/start');self.assertTrue(sent['input'][0]['text'].endswith(prompt))
