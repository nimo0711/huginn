import concurrent.futures,contextlib,io,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import board_actions as actions

class ConfigTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.home=self.root/'home';self.home.mkdir();self.cwd=self.root/'project';self.cwd.mkdir()
  self.store=actions.ActionStore(self.root/'state',self.home)
 def skill(self,name):
  p=self.home/'.claude/skills'/name/'SKILL.md';p.parent.mkdir(parents=True);p.write_text('---\nname: '+name+'\ndescription: Test\n---\nInstructions');return p
 def test_defaults_do_not_choose_a_skill_or_terminal_product(self):
  self.skill('custom-report')
  self.assertEqual(self.store.settings(),{'auto_servers':True,'auto_terminal':True,'launch_app':''})
  self.assertEqual(self.store.shortcuts(self.cwd),[])
 def test_configuration_persists_and_rejects_invalid_values(self):
  self.store.configure({'auto_terminal':False})
  other=actions.ActionStore(self.root/'state',self.home)
  self.assertFalse(other.settings()['auto_terminal'])
  for changes in [{'auto_servers':'yes'},{'command':'echo unsafe'},{'launch_app':'/not/an/app'}]:
   with self.assertRaises(ValueError):self.store.configure(changes)
 def test_removed_skill_can_be_unregistered_and_label_is_preserved(self):
  p=self.skill('custom-report');sid=self.store.catalog(self.cwd)[0]['id']
  self.store.favorite(sid,True,self.cwd,'맞춤 보고')
  self.store.favorite(sid,True,self.cwd)
  self.assertEqual(self.store.shortcuts(self.cwd)[0]['label'],'맞춤 보고')
  p.unlink();self.assertFalse(self.store.shortcuts(self.cwd)[0]['available'])
  self.store.favorite(sid,False,self.cwd);self.assertEqual(self.store.shortcuts(self.cwd),[])
 def test_independent_stores_do_not_lose_concurrent_registrations(self):
  for i in range(12):self.skill('task-'+str(i))
  ids=[x['id'] for x in self.store.catalog(self.cwd)]
  def add(sid):actions.ActionStore(self.root/'state',self.home).favorite(sid,True,self.cwd)
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(add,ids))
  self.assertEqual({x['id'] for x in self.store.shortcuts(self.cwd)},set(ids))
 @unittest.skipIf(sys.platform == 'win32', 'requires macOS terminal application association')
 def test_launcher_uses_system_association_or_configured_app(self):
  with patch.object(actions.subprocess,'run') as run:
   run.return_value.returncode=0
   actions.open_terminal(Path('/tmp/test.command'))
   self.assertEqual(run.call_args.args[0],['/usr/bin/open','/tmp/test.command'])
   actions.open_terminal(Path('/tmp/test.command'),'/Applications/Custom.app')
   self.assertEqual(run.call_args.args[0],['/usr/bin/open','-a','/Applications/Custom.app','/tmp/test.command'])
 def test_cli_registers_without_launching_and_shares_ui_storage(self):
  import actions_cli
  self.skill('custom-report');sid=self.store.catalog(self.cwd)[0]['id']
  out=io.StringIO()
  with contextlib.redirect_stdout(out),patch.object(actions,'open_terminal') as launch:
   self.assertEqual(actions_cli.main(['add','--skill',sid,'--label','맞춤 보고','--cwd',str(self.cwd)],store=self.store),0)
   self.assertEqual(actions_cli.main(['list','--cwd',str(self.cwd)],store=self.store),0)
   launch.assert_not_called()
  self.assertEqual(self.store.shortcuts(self.cwd)[0]['label'],'맞춤 보고')
  self.assertIn('맞춤 보고',out.getvalue())
 def test_help_explains_registration_and_does_not_expose_tokens(self):
  guide=self.store.guide(self.cwd)
  self.assertIn('actions add',guide);self.assertIn('actions list',guide)
  self.assertIn('실행하지',guide);self.assertNotIn('X-Work-Board-Token',guide)
