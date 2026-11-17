import contextlib
import io
import json
import tempfile
import unittest
import threading
from pathlib import Path
from unittest.mock import patch
import board_actions
import actions_cli


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.store = board_actions.ActionStore(self.root/'state', self.root)

    def test_manual_and_cli_share_registered_connection(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = actions_cli.main(['connections','save','--kind','jira','--values',
                json.dumps({'enabled': True, 'site': 'https://example.atlassian.net', 'project': 'ABC'})], store=self.store)
        self.assertEqual(code, 0)
        rows = self.store.connections.catalog()
        row = next(x for x in rows if x['kind']=='jira')
        self.assertTrue(row['registered']);self.assertEqual(row['values']['project'], 'ABC')
        self.assertEqual(board_actions.ActionStore(self.root/'state',self.root).connections.get('jira')['site'], 'https://example.atlassian.net')

    def test_invalid_or_secret_configuration_leaves_existing_data_unchanged(self):
        conn = self.store.connections
        conn.save('jira', {'enabled': True, 'site': 'https://example.atlassian.net', 'project': 'ABC'})
        original = conn.get('jira')
        for values in [{'site':'https://user:secret@example.com'}, {'token':'secret'},
                       {'enabled':'yes'}, {'project':'ABC OR project = OTHER'}, {'site':'javascript:alert(1)'}]:
            with self.subTest(values=values), self.assertRaises(ValueError):conn.save('jira', values)
        self.assertEqual(conn.get('jira'), original)

    def test_remove_does_not_touch_other_connections_or_auth(self):
        conn=self.store.connections
        conn.save('claude', {'enabled':True});conn.save('codex', {'enabled':True})
        with patch('subprocess.run') as run:conn.remove('claude')
        self.assertFalse(conn.get('claude')['enabled']);self.assertTrue(conn.get('codex')['enabled']);run.assert_not_called()

    def test_cli_help_uses_schema_and_has_readback_and_check_commands(self):
        text=self.store.guide(self.root)
        for term in ['connections list', 'connections save', 'connections check', 'connections remove', 'gitlab', 'summary']:
            self.assertIn(term,text)
        self.assertNotIn('X-Work-Board-Token',text)

    def test_git_roots_are_existing_directories_and_preserve_multiple_values(self):
        a=self.root/'a';b=self.root/'b';a.mkdir();b.mkdir()
        self.store.connections.save('git', {'roots':[str(a),str(b)]})
        self.assertEqual(self.store.connections.get('git')['roots'],[str(a.resolve()),str(b.resolve())])
        with self.assertRaises(ValueError):self.store.connections.save('git', {'roots':[str(self.root/'missing')]})

    def test_disabled_sources_hide_cached_results_without_queries(self):
        import connections, enrich, sprint, scan
        self.store.connections.save('jira', {'enabled':False})
        self.store.connections.save('gitlab', {'enabled':False})
        self.store.connections.save('git', {'enabled':False})
        with patch.object(connections,'STORE',self.store.connections), patch.object(enrich,'_run') as run:
            with patch.dict(enrich._data, {'ABC-1':{'title':'stale','mrs':[{'iid':1}],'commits':[{}]}}):
                value=enrich.story('ABC-1')
                self.assertFalse(value.get('title'));self.assertFalse(value.get('mrs'));self.assertFalse(value.get('commits'))
            self.assertEqual(sprint.summary().get('items',[]),[])
            self.assertEqual(scan._jira_title('ABC-1'),'');run.assert_not_called()

    def test_gitlab_query_uses_saved_host_group_and_folder(self):
        import connections,enrich
        self.store.connections.save('gitlab', {'enabled':True,'host':'git.example.test:8443','group':'team/sub','cwd':str(self.root)})
        with patch.object(connections,'STORE',self.store.connections),patch.object(enrich,'_run',return_value='[]'),patch.object(connections,'run',return_value=(0,'[]')) as run:
            enrich._mrs('ABC-1')
        self.assertTrue(run.called, 'port-bearing hosts must use the shared GitLab transport')
        args=run.call_args.args
        self.assertIn('groups/team%2Fsub/merge_requests?',args[0][2]);self.assertNotIn('--hostname',args[0]);self.assertEqual(args[1],str(self.root.resolve()))
        self.assertEqual(run.call_args.kwargs['env']['GITLAB_HOST'],'git.example.test:8443')

    def test_git_finds_worktrees_in_hidden_containers(self):
        import connections,enrich
        repo=self.root/'.worktrees'/'feature';repo.mkdir(parents=True);(repo/'.git').write_text('gitdir: /example')
        self.store.connections.save('git',{'enabled':True,'roots':[str(self.root)]})
        with patch.object(connections,'STORE',self.store.connections),patch.object(enrich,'_run',return_value='100|[ABC-1] change') as run:
            commits=enrich._commits('ABC-1')
        self.assertEqual(len(commits),1);self.assertEqual(run.call_args.args[0][2],str(repo.resolve()))

    def test_disabled_agent_is_not_scanned(self):
        import connections,scan
        self.store.connections.save('claude', {'enabled':False});self.store.connections.save('codex', {'enabled':True})
        with patch.object(connections,'STORE',self.store.connections),patch.object(scan,'claude_sessions',return_value=[]) as claude,patch.object(scan,'codex_sessions',return_value=[]) as codex:
            scan.all_sessions();claude.assert_not_called();codex.assert_called_once()

    def test_queued_summary_is_cancelled_when_disabled_or_model_changed(self):
        import connections,title_summary
        for change in ({'enabled':False},{'model':'sonnet'}):
            with self.subTest(change=change):
                self.store.connections.save('summary',{'enabled':True,'model':'haiku'})
                entered=threading.Event();release=threading.Event();calls=[]
                def runner(context):
                    calls.append(context)
                    if len(calls)==1:entered.set();release.wait(2)
                    return '요약 결과'
                with patch.object(connections,'STORE',self.store.connections):
                    cache=title_summary.TitleCache(self.root/(next(iter(change))+'.json'),runner)
                    cache.get({'request':'first'});self.assertTrue(entered.wait(1))
                    cache.get({'request':'second'});self.store.connections.save('summary',change)
                    release.set();cache.jobs.join()
                    self.assertEqual(len(calls),1)
                    self.assertFalse(cache.entries)

    def test_jira_account_switch_is_checked_again_before_next_query(self):
        import connections
        with patch.object(connections,'run',side_effect=[(0,'site-a.atlassian.net'),(0,'site-b.atlassian.net')]):
            self.assertTrue(connections.jira_account_matches('https://site-a.atlassian.net'))
            self.assertFalse(connections.jira_account_matches('https://site-a.atlassian.net'))

    def test_disabled_providers_hide_usage_and_stop_reading_logs(self):
        import connections,usage
        self.store.connections.save('claude',{'enabled':False})
        self.store.connections.save('codex',{'enabled':False})
        with patch.object(connections,'STORE',self.store.connections),patch.dict(usage._result,{'days':[{'tokens':123}]}),patch.object(usage.glob,'glob') as files:
            self.assertFalse(usage.summary().get('days'))
            usage._refresh();files.assert_not_called()


if __name__=='__main__':unittest.main()
