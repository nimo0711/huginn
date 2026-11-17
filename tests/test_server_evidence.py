import json,sqlite3,unittest
import scan

class ServerEvidenceTests(unittest.TestCase):
 def test_claude_server_start_output_keeps_timestamp_but_failed_output_does_not(self):
  info=scan._new_info();scan._open_task(info,'Start preview',100)
  for error,port in [(True,3000),(False,3001)]:
   scan._feed(info,json.dumps({'type':'assistant','timestamp':'2026-11-16T03:59:59Z','message':{'content':[{'type':'tool_use','id':'shell','name':'Bash','input':{'command':'npm run dev'}}]}}))
   scan._feed(info,json.dumps({'type':'user','timestamp':'2026-11-16T04:00:00Z','message':{'content':[{'type':'tool_result','tool_use_id':'shell','is_error':error,'content':f'Local: http://localhost:{port}/'}]}}))
  urls=info['tasks'][-1]['servers']
  self.assertEqual([x['url'] for x in urls],['http://127.0.0.1:3001/']);self.assertGreater(urls[0]['ts'],100)
 def test_codex_command_output_is_used_but_user_links_are_not(self):
  con=sqlite3.connect(':memory:');self.addCleanup(con.close)
  con.execute('CREATE TABLE thread_items (thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal)')
  rows=[('userMessage',{'content':[{'text':'Local: http://localhost:3000/'}]}),('commandExecution',{'command':'npm run dev','exitCode':None,'aggregatedOutput':'Local: http://localhost:3001/'})]
  for i,(kind,body) in enumerate(rows):con.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('s','t',kind,json.dumps(body),100000+i*1000,i))
  task=scan._codex_turn(con,'s','t','inProgress',100,0)
  self.assertEqual(task['servers'],[{'url':'http://127.0.0.1:3001/','ts':101,'start_ts':101}])
