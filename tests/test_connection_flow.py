import json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import connections,board_actions

def response(out='',code=0,err=''):
 return SimpleNamespace(stdout=out,stderr=err,returncode=code)

class ConnectionFlowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.store=board_actions.ActionStore(self.root/'state',self.root);self.addCleanup(self.store.delivery.close);self.conn=self.store.connections
  self.conn.save('jira',{'enabled':True,'site':'https://test.atlassian.net','project':'ABC'})
 def test_auth_alone_is_not_project_access(self):
  with patch('connections.subprocess.run',side_effect=[response('test.atlassian.net'),response(code=1,err='HTTP 403 Forbidden')]) as run:
   value=self.conn.check('jira')
  self.assertFalse(value['ok']);self.assertEqual(value['status'],'permission_denied')
  self.assertIn('project',run.call_args.args[0]);self.assertIn('ABC',run.call_args.args[0])
 def test_success_queries_project_and_rechecks_site(self):
  with patch('connections.subprocess.run',side_effect=[response('test.atlassian.net'),response('{"id":"100","key":"ABC"}'),response('test.atlassian.net')]):value=self.conn.check('jira')
  self.assertEqual(value['status'],'connected')
  saved=board_actions.ActionStore(self.root/'state',self.root).connections.catalog()
  self.assertEqual(next(x for x in saved if x['kind']=='jira')['check']['status'],'connected')
  self.conn.save('jira',{'project':'OTHER'})
  self.assertIsNone(next(x for x in self.conn.catalog() if x['kind']=='jira')['check'])
 def test_missing_cli_login_and_network_are_different(self):
  cases=[(FileNotFoundError(),'cli_missing'),(response(code=1,err='You are not logged in'),'login_required'),(response(code=1,err='dial tcp: no such host'),'network_error'),(response('other.atlassian.net'),'site_mismatch')]
  for outcome,status in cases:
   with self.subTest(status=status),patch('connections.subprocess.run',side_effect=outcome if isinstance(outcome,Exception) else None,return_value=outcome):
    self.assertEqual(self.conn.check('jira')['status'],status)
 def test_disabled_connection_is_not_reported_connected(self):
  self.conn.save('jira',{'enabled':False})
  with patch('connections.subprocess.run') as run:
   self.assertEqual(self.conn.check('jira')['status'],'disabled');run.assert_not_called()
 def test_config_change_during_check_does_not_publish_old_success(self):
  count=0
  def runner(*a,**kw):
   nonlocal count
   count+=1
   if count==2:self.conn.save('jira',{'project':'OTHER'});return response('{"id":"100","key":"ABC"}')
   return response('test.atlassian.net')
  with patch('connections.subprocess.run',side_effect=runner):value=self.conn.check('jira')
  self.assertFalse(value['ok']);self.assertEqual(value['status'],'config_changed')
 def test_partial_edit_preserves_ai_update_and_conflicting_edit_is_rejected(self):
  original=self.conn.get('jira');self.conn.save('jira',{'project':'OTHER'})
  value=self.conn.save('jira',{'enabled':False},expected={'enabled':original['enabled']})
  self.assertEqual(value['project'],'OTHER')
  with self.assertRaisesRegex(ValueError,'변경'):
   self.conn.save('jira',{'project':'THIRD'},expected={'project':original['project']})
  self.assertEqual(self.conn.get('jira')['project'],'OTHER')
 def test_gitlab_checks_user_and_group_without_changing_host_config(self):
  self.conn.save('gitlab',{'enabled':True,'host':'git.example.test:8443','group':'team/sub','cwd':str(self.root)})
  with patch('connections.subprocess.run',side_effect=[response('{"id":1}'),response('{"id":2}')]) as run:value=self.conn.check('gitlab')
  self.assertEqual(value['status'],'connected');self.assertEqual(run.call_count,2)
  self.assertEqual(run.call_args.kwargs['env']['GITLAB_HOST'],'git.example.test:8443')
  self.assertIn('groups/team%2Fsub',run.call_args.args[0])
 def test_secret_errors_never_escape_check(self):
  with patch('connections.subprocess.run',return_value=response(code=1,err='HTTP 401 secret-user glpat-private-value')):value=self.conn.check('jira')
  self.assertNotIn('private',json.dumps(value));self.assertNotIn('secret-user',self.conn.check_path.read_text())
 def test_login_script_targets_registered_site_and_never_stores_credentials(self):
  with patch('connections.cli', return_value=sys.executable), patch('board_actions.open_terminal') as opened:
   result=self.store.login('jira','login-request-0001')
  path=Path(opened.call_args.args[0])
  script=path.read_text(encoding='utf-8')
  if sys.platform == 'win32':
   job=json.loads(script)
   self.assertEqual(job['exe'],sys.executable)
   self.assertEqual(job['cwd'],str(self.root))
   self.assertEqual(job['removeEnv'],[])
  self.assertIn('test.atlassian.net',script);self.assertIn('auth login --web',script)
  self.assertEqual(result['status'],'login_opened');self.assertNotIn('--token',script)
  with patch('connections.cli', return_value=sys.executable), patch('board_actions.open_terminal') as opened:
   self.store.login('jira','login-request-0001');opened.assert_not_called()
 def test_ai_guide_explains_manual_auth_and_separate_verification(self):
  guide=self.conn.guide('wb')
  for word in ('로그인','연결 확인','비밀번호','CLI','project'):self.assertIn(word,guide)

 def test_missing_cli_never_executes_repository_binary(self):
  self.conn.save('gitlab',{'enabled':True,'host':'example.test:8443','group':'team','cwd':str(self.root)})
  (self.root/'glab').write_text('#!/bin/sh\nexit 0\n');(self.root/'glab').chmod(0o700)
  with patch('connections.cli',return_value='glab'),patch('board_actions.open_terminal') as opened:
   with self.assertRaisesRegex(ValueError,'CLI'):self.store.login('gitlab','missing-cli-test')
   opened.assert_not_called()
 def test_login_target_is_an_argument_even_with_a_port(self):
  self.conn.save('gitlab',{'enabled':True,'host':'example.test:8443','group':'team','cwd':str(self.root)})
  args,_,_,_=self.conn.login_command('gitlab');self.assertEqual(args[args.index('--hostname')+1],'example.test:8443')
  args,_,_,_=self.conn.login_command('jira');self.assertEqual(args[args.index('--site')+1],'test.atlassian.net')
 def test_final_jira_auth_failure_retains_its_cause(self):
  with patch('connections.subprocess.run',side_effect=[response('test.atlassian.net'),response('{"id":"100","key":"ABC"}'),response(code=1,err='dial tcp: no such host')]):
   self.assertEqual(self.conn.check('jira')['status'],'network_error')

if __name__=='__main__':unittest.main()
