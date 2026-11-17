import unittest,tempfile,time
from unittest.mock import patch
from pathlib import Path
import usage

class UsageStatusTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.enabled=lambda name:{'enabled':name=='claude'}
 def test_missing_and_empty_sources_are_not_zero_usage(self):
  for path in (self.root/'missing',self.root):
   with self.subTest(path=path),patch.object(usage,'PROJECTS',str(path)),patch.object(usage.connections.STORE,'get',side_effect=self.enabled),patch.dict(usage._result,{},clear=True):
    usage._refresh();self.assertTrue(usage._result['unavailable']);self.assertNotIn('at',usage._result);self.assertNotIn('today',usage._result)
 def test_read_error_preserves_timestamp_and_flags_old_value(self):
  (self.root/'s.jsonl').write_text('{}\n')
  previous={'provider':'claude','at':123,'today':456,'week':456,'stats':{'daily':[],'models':[],'tools':[],'projects':[],'hours':[0]*24,'week_sessions':1}}
  with patch.object(usage,'PROJECTS',str(self.root)),patch.object(usage.connections.STORE,'get',side_effect=self.enabled),patch.dict(usage._result,previous,clear=True),patch.object(usage,'_scan_file',side_effect=PermissionError):
   usage._refresh();self.assertEqual(usage._result['at'],123);self.assertEqual(usage._result['today'],456);self.assertTrue(usage._result['stale']);self.assertTrue(usage._result['error']);self.assertFalse(usage._busy.is_set())
 def test_recent_failure_does_not_retry_every_poll(self):
  with patch.object(usage.connections.STORE,'get',side_effect=self.enabled),patch.dict(usage._result,{'attempt_at':time.time(),'error':'error'},clear=True),patch.object(usage.threading,'Thread') as thread:
   usage.summary();thread.assert_not_called()
 def test_updated_usage_and_repeated_tool_keep_final_counts(self):
  import json
  p=self.root/'usage.jsonl';event={'type':'assistant','timestamp':'2026-11-17T03:00:00Z','message':{'id':'message1','model':'claude','usage':{'input_tokens':100,'output_tokens':1},'content':[{'type':'tool_use','id':'tool1','name':'Bash'}]}}
  first=json.dumps(event)+'\n';event['message']['usage']['output_tokens']=20
  p.write_text(first+json.dumps(event)+'\n')
  with patch.dict(usage._files,{},clear=True):
   usage._scan_file(str(p),p.stat().st_size);st=usage._files[str(p)]
   self.assertEqual(sum(st['days'].values()),120);self.assertEqual(sum(st['s']['out'].values()),20);self.assertEqual(sum(sum(x.values()) for x in st['s']['tools'].values()),1)
   with p.open('a') as f:f.write(json.dumps(event)+'\n')
   usage._scan_file(str(p),p.stat().st_size);self.assertEqual(sum(st['days'].values()),120)
