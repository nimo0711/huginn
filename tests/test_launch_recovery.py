import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import board_actions

class LaunchRecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.state=self.root/'state'
  skill=self.root/'.codex/skills/.system/test-skill/SKILL.md';skill.parent.mkdir(parents=True);skill.write_text('---\nname: test-skill\ndescription: test\n---\nTest only')
  self.store=board_actions.ActionStore(self.state,self.root);self.addCleanup(self.store.delivery.close)
 def test_system_skills_are_discovered(self):
  self.assertEqual([s['name'] for s in self.store.catalog(self.root)],['test-skill'])
 def test_restart_reuses_launch_receipt_instead_of_opening_again(self):
  with patch('board_actions.executable',return_value='/bin/echo'),patch('board_actions.open_terminal') as opened:
   item=self.store.catalog(self.root)[0];first=self.store.launch(item['id'],self.root,'durable-request')
   other=board_actions.ActionStore(self.state,self.root);self.addCleanup(other.delivery.close)
   second=other.launch(item['id'],self.root,'durable-request');self.assertEqual(first['id'],second['id']);self.assertEqual(opened.call_count,1)
 def test_interrupted_launch_does_not_open_a_second_window(self):
  with patch('board_actions.executable',return_value='/bin/echo'),patch('board_actions.open_terminal'):
   item=self.store.catalog(self.root)[0];self.store.launch(item['id'],self.root,'interrupted-request')
  path=self.state/'launch-requests.json';records=json.loads(path.read_text());next(iter(records.values()))['status']='pending';path.write_text(json.dumps(records))
  with patch('board_actions.open_terminal') as opened:
   result=board_actions.ActionStore(self.state,self.root).launch(item['id'],self.root,'interrupted-request');self.assertEqual(result['status'],'unknown');opened.assert_not_called()
 def test_open_timeout_is_unknown_and_same_request_never_opens_again(self):
  import subprocess
  with patch('board_actions.executable',return_value='/bin/echo'),patch('board_actions.open_terminal',side_effect=subprocess.TimeoutExpired('open',10)) as opened:
   item=self.store.catalog(self.root)[0];first=self.store.launch(item['id'],self.root,'timeout-request');second=self.store.launch(item['id'],self.root,'timeout-request')
   self.assertEqual(first['status'],'unknown');self.assertEqual(second['status'],'unknown');self.assertEqual(opened.call_count,1)
