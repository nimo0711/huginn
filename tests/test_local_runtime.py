import unittest
import sys
from unittest.mock import patch
import local_runtime as runtime

class RuntimeTests(unittest.TestCase):
 @unittest.skipIf(sys.platform == 'win32', 'requires macOS lsof discovery')
 def test_listener_addresses_are_kept_and_lan_only_binding_is_excluded(self):
  output='p11\ntIPv6\nn[::1]:5173\np21\ntIPv4\nn127.0.0.1:5173\nn192.168.0.2:3000\nn*:8000\n'
  with patch.object(runtime,'run',return_value=output):ports=runtime.listeners()
  self.assertEqual(ports,[{'pid':11,'port':5173,'host':'::1'},{'pid':21,'port':5173,'host':'127.0.0.1'},{'pid':21,'port':8000,'host':'127.0.0.1'}])
 @unittest.skipIf(sys.platform == 'win32', 'requires macOS lsof discovery')
 def test_codex_shared_transcripts_only_identify_terminal_not_server_owner(self):
  a='00000000-0000-0000-0000-000000000001';b='00000000-0000-0000-0000-000000000002'
  procs={10:{'ppid':1,'comm':'/bin/codex'},11:{'ppid':10,'comm':'node'}}
  output=f'p10\nn/home/.codex/sessions/2026/rollout-date-{a}.jsonl\nn/home/.codex/sessions/2026/rollout-date-{b}.jsonl'
  terminal={'path':'/Applications/Terminal.app','app':'Terminal'}
  with patch.object(runtime,'run',return_value=output),patch.object(runtime,'terminal_for',return_value=terminal):
   self.assertEqual(runtime.codex_terminals(procs,{a,b}),{a:terminal,b:terminal})
   self.assertEqual(runtime.codex_roots(procs,{a,b}),{})
 def test_urls_are_only_loopback_without_credentials_or_redirect_hosts(self):
  self.assertEqual(runtime.local_url('http://0.0.0.0:5173/'), 'http://127.0.0.1:5173/')
  self.assertEqual(runtime.local_url('https://localhost:9443/view'), 'https://127.0.0.1:9443/view')
  for text in ['http://localhost.attacker:5173','http://u:p@localhost:5173','file:///tmp/x','http://192.0.2.1','javascript:alert(1)']:
   self.assertIsNone(runtime.local_url(text))
 def test_startup_output_not_prompt_or_ordinary_link_identifies_a_server(self):
  self.assertEqual(runtime.startup_urls('  ➜  Local:   http://localhost:5173/'),['http://127.0.0.1:5173/'])
  self.assertEqual(runtime.startup_urls('Read http://localhost:5173/ to review'),[])
 def test_nearest_session_owner_wins_for_nested_agents(self):
  procs={1:{'ppid':0},10:{'ppid':1},20:{'ppid':10},30:{'ppid':20}}
  self.assertEqual(runtime.owner(30,procs,{10:'parent',20:'child'}),'child')
  self.assertIsNone(runtime.owner(1,procs,{10:'parent'}))
 def test_only_http_listener_attaches_to_its_owner_and_disappears_on_exit(self):
  sessions=[{'id':'a','pid':10,'tasks':[]},{'id':'b','pid':20,'tasks':[]}]
  procs={10:{'ppid':1,'comm':'claude'},11:{'ppid':10,'comm':'node'},20:{'ppid':1,'comm':'claude'}}
  with patch.object(runtime,'http_ready',side_effect=lambda url: ':5173' in url):
   result=runtime.resolve(sessions,procs,[{'pid':11,'port':5173},{'pid':11,'port':5432}],{})
   self.assertEqual([x['port'] for x in result['a']['servers']],[5173])
   self.assertEqual(result['b']['servers'],[])
   self.assertEqual(runtime.resolve(sessions,procs,[],{})['a']['servers'],[])
 def test_codex_startup_evidence_requires_a_matching_live_listener(self):
  sessions=[{'id':'c','tasks':[{'servers':['http://127.0.0.1:3000/']}]}]
  with patch.object(runtime,'http_ready',return_value=True):
   self.assertEqual(runtime.resolve(sessions,{},[],{})['c']['servers'],[])
   self.assertEqual(runtime.resolve(sessions,{},[{'pid':12,'port':3000}],{})['c']['servers'],[])
   sessions[0]['tasks'][0]['servers']=[{'url':'http://127.0.0.1:3000/','start_ts':100,'ts':101}]
   self.assertEqual(runtime.resolve(sessions,{12:{'started':100}},[{'pid':12,'port':3000}],{})['c']['servers'],[])
 def test_daemon_shared_by_several_threads_is_not_itself_a_session_owner(self):
  procs={1:{'ppid':0},10:{'ppid':1},11:{'ppid':10},12:{'ppid':10}}
  self.assertIsNone(runtime.owner(12,procs,{11:'thread-a'}))
 def test_ipv4_and_ipv6_same_port_keep_their_own_address(self):
  procs={10:{'ppid':1,'comm':'claude'},11:{'ppid':10,'comm':'node'},20:{'ppid':1,'comm':'claude'},21:{'ppid':20,'comm':'node'}}
  sessions=[{'id':'a','pid':10},{'id':'b','pid':20}]
  ports=[{'pid':11,'port':5173,'host':'::1'},{'pid':21,'port':5173,'host':'127.0.0.1'}]
  with patch.object(runtime,'http_ready',return_value=True):r=runtime.resolve(sessions,procs,ports,{})
  self.assertEqual(r['a']['servers'][0]['url'],'http://[::1]:5173/')
  self.assertEqual(r['b']['servers'][0]['url'],'http://127.0.0.1:5173/')
 def test_stale_startup_record_never_binds_a_reused_port(self):
  sessions=[{'id':'a','tasks':[{'servers':[{'url':'http://127.0.0.1:3000/','ts':100}]}]}]
  with patch.object(runtime,'http_ready',return_value=True):
   r=runtime.resolve(sessions,{12:{'ppid':1,'started':200}},[{'pid':12,'port':3000,'host':'127.0.0.1'}],{})
  self.assertEqual(r['a']['servers'],[])
 def test_agent_internal_http_port_is_not_a_user_server(self):
  with patch.object(runtime,'http_ready',return_value=True):
   r=runtime.resolve([{'id':'a','pid':10}],{10:{'ppid':1,'comm':'claude'}},[{'pid':10,'port':54416,'host':'127.0.0.1'}],{})
  self.assertEqual(r['a']['servers'],[])
