import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import board_actions as actions
import local_runtime as runtime
import scan

class IdentityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.home=self.root/'home';self.a=self.home/'a';self.b=self.home/'b';self.a.mkdir(parents=True);self.b.mkdir()
  self.store=actions.ActionStore(self.root/'state',self.home)
 def skill(self,root,agent='claude',folder='review',meta=''):
  p=root/('.'+agent)/'skills'/folder/'SKILL.md';p.parent.mkdir(parents=True,exist_ok=True);p.write_text('---\nname: review\n'+meta+'\n---\nReview instructions');return p
 def test_common_registration_preserves_identity_in_every_project(self):
  for agent in ('claude','codex'):self.skill(self.home,agent)
  common=self.store.catalog(self.home)
  for item in common:self.store.favorite(item['id'],True,self.home,'공통 검토')
  self.assertEqual({x['id'] for x in common},{x['id'] for x in self.store.catalog(self.a)})
  self.assertTrue(all(x['favorite'] for x in self.store.catalog(self.a)))
 def test_project_button_never_rebinds_to_a_different_file(self):
  for agent in ('claude','codex'):self.skill(self.a,agent);self.skill(self.b,agent)
  a=self.store.catalog(self.a);b=self.store.catalog(self.b)
  self.assertTrue({x['id'] for x in a}.isdisjoint(x['id'] for x in b))
  for item in a:
   self.store.favorite(item['id'],True,self.a,'A 검토')
   with self.assertRaises(ValueError):self.store.launch(item['id'],self.b,'wrong-project')
  self.assertFalse(any(x['favorite'] for x in self.store.catalog(self.b)))
 def test_same_metadata_name_does_not_hide_distinct_installed_files(self):
  self.skill(self.home,folder='one');self.skill(self.home,folder='two')
  self.assertEqual(len({x['id'] for x in self.store.catalog(self.a)}),2)
 def test_yaml_inline_false_excludes_noninvocable_skill(self):
  self.skill(self.home,meta='user-invocable: false # internal only')
  self.assertEqual(self.store.catalog(self.a),[])

class ServerProvenanceTests(unittest.TestCase):
 def feed(self,info,kind,body,t):
  scan._feed(info,json.dumps({'type':kind,'timestamp':f'2026-11-16T04:00:{t:02d}Z','message':{'content':body}}))
 def test_document_read_output_is_not_a_server_start(self):
  info=scan._new_info();scan._open_task(info,'read docs',1)
  self.feed(info,'assistant',[{'type':'tool_use','id':'read','name':'Read','input':{'file_path':'README.md'}}],1)
  self.feed(info,'user',[{'type':'tool_result','tool_use_id':'read','content':'Local: http://localhost:3000/'}],2)
  self.assertEqual(info['tasks'][-1]['servers'],[])
 def codex(self,commands):
  db=sqlite3.connect(':memory:');self.addCleanup(db.close)
  db.execute('CREATE TABLE thread_items (thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal,started_at_ms,completed_at_ms)')
  for i,(cmd,start,end) in enumerate(commands):
   d={'command':cmd,'exitCode':0,'aggregatedOutput':'Local: http://localhost:3000/'}
   db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?,?,?)',('s','t','commandExecution',json.dumps(d),end*1000,i,start*1000,end*1000))
  return scan._codex_turn(db,'s','t','inProgress',1,0)['servers']
 def test_codex_read_commands_do_not_establish_ownership(self):
  self.assertEqual(self.codex([('cat README.md',10,11)]),[])
 def test_latest_restart_updates_both_launch_and_result_times(self):
  records=self.codex([('npm run dev',100,101),('npm run dev',200,201)])
  self.assertEqual(len(records),1);self.assertEqual(records[0]['ts'],201);self.assertEqual(records[0]['start_ts'],200)
 def test_detached_process_must_start_during_this_launch(self):
  s=[{'id':'s','tasks':[{'servers':[{'url':'http://127.0.0.1:3000/','start_ts':100,'ts':101}]}]}]
  with patch.object(runtime,'http_ready',return_value=True):
   for start,expected in [(50,False),(102,False),(100,False)]:
    result=runtime.resolve(s,{12:{'ppid':1,'comm':'node','started':start}},[{'pid':12,'port':3000,'host':'127.0.0.1'}],{})
    self.assertEqual(bool(result['s']['servers']),expected)
 def test_server_from_a_verified_codex_thread_family_remains_visible(self):
  procs={10:{'ppid':1,'comm':'codex'},12:{'ppid':10,'comm':'node','started':100}}
  with patch.object(runtime,'run',return_value=''),patch.object(runtime,'codex_open_threads',return_value={10:{'parent','child'}}),patch.object(runtime,'codex_thread_roots',return_value={'parent':'parent','child':'parent'}),patch.object(runtime,'http_ready',return_value=True):
   roots=runtime.codex_roots(procs,{'parent'});self.assertEqual(roots,{10:'parent'})
   result=runtime.resolve([{'id':'parent','agent':'codex'}],procs,[{'pid':12,'port':3000}],roots)
   self.assertEqual([s['port'] for s in result['parent']['servers']],[3000])
 def test_mixed_codex_thread_families_do_not_claim_a_shared_server(self):
  with patch.object(runtime,'run',return_value=''),patch.object(runtime,'codex_open_threads',return_value={10:{'a','b'}}),patch.object(runtime,'codex_thread_roots',return_value={'a':'a','b':'b'}):
   self.assertEqual(runtime.codex_roots({10:{'comm':'codex'}},{'a','b'}),{})
