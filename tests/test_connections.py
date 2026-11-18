import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import board_actions
import actions_cli


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.store = board_actions.ActionStore(self.root/'state', self.root)
        self.addCleanup(self.store.delivery.close)

    def test_fresh_install_does_not_collect_existing_history_or_generate_titles(self):
        import connections, scan, title_summary, usage
        with patch.object(connections, 'STORE', self.store.connections), \
                patch.object(scan, 'BOARD_DB', self.root/'board.db'), \
                patch.object(scan.glob, 'glob', side_effect=AssertionError('source history read before connection')), \
                patch.object(scan, 'codex_sessions', side_effect=AssertionError('Codex opened before connection')):
            self.assertEqual(scan.all_sessions(), [])
            self.assertTrue(usage.summary()['disabled'])
            usage._refresh()
            cache=title_summary.TitleCache(self.root/'titles.json', lambda _: self.fail('summary ran before consent'))
            self.assertEqual(cache.get({'requests':['existing work']}), {'state':'disabled','title':''})
            self.assertIsNone(cache.worker)
        self.assertTrue(all(not row['registered'] for row in self.store.connections.catalog()))
        self.assertFalse(self.store.connections.path.exists())

    def test_saving_claude_starts_collection_without_changing_source_files(self):
        import connections, scan
        home=self.root/'다른 사용자';project=home/'.claude/projects/project';project.mkdir(parents=True)
        self.store.connections.home=home
        source=project/'first-session.jsonl'
        source.write_text(json.dumps({'type':'user','cwd':str(home/'내 작업'),'timestamp':datetime.now(timezone.utc).isoformat(),
            'message':{'role':'user','content':'첫 연결 상태를 확인해 주세요.'}},ensure_ascii=False)+'\n')
        auth=home/'.claude/settings.json';auth.write_text('{"userSetting":"keep"}')
        snapshots={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in (source,auth)}
        with patch.object(connections, 'STORE', self.store.connections), \
                patch.object(scan, 'BOARD_DB', self.root/'board.db'), \
                patch.object(scan, 'CLAUDE_PROJECTS', str(home/'.claude/projects')), \
                patch.object(scan, 'CLAUDE_SESSIONS', str(home/'.claude/sessions')), \
                patch.object(scan, 'CODEX_STATE', str(home/'.codex/state_5.sqlite')):
            self.assertEqual(scan.all_sessions(), [])
            self.store.connections.save('claude', {'enabled':True})
            rows=scan.all_sessions()
            self.assertEqual([row['id'] for row in rows], ['first-session'])
            self.assertEqual(rows[0]['cwd_path'], str(home/'내 작업'))
            self.assertEqual(rows[0]['works'][0]['title'], '')
        self.assertEqual({path:(path.read_bytes(),path.stat().st_mtime_ns) for path in snapshots}, snapshots)
        self.assertEqual(self.store.connections.get('git')['roots'], [])
        self.assertFalse(self.store.connections.get('summary')['enabled'])

    def test_explicit_existing_connection_settings_survive_new_defaults(self):
        original={'claude':{'enabled':True},'codex':{'enabled':False,'registered':True},
                  'summary':{'enabled':True,'model':'sonnet'},'git':{'enabled':True,'roots':[str(self.root)]}}
        self.store.connections.state.mkdir(parents=True)
        self.store.connections.path.write_text(json.dumps(original))
        before=self.store.connections.path.read_bytes()
        rows={row['kind']:row for row in self.store.connections.catalog()}
        self.assertTrue(rows['claude']['values']['enabled']);self.assertTrue(rows['claude']['registered'])
        self.assertFalse(rows['codex']['values']['enabled']);self.assertTrue(rows['codex']['registered'])
        self.assertEqual(rows['summary']['values'], {'enabled':True,'model':'sonnet'})
        self.assertEqual(rows['git']['values']['roots'], [str(self.root)])
        self.assertFalse(rows['jira']['registered'])
        self.assertEqual(self.store.connections.path.read_bytes(), before)

    def test_missing_source_check_names_required_location_without_creating_it(self):
        for kind,expected in [('claude', self.root/'.claude/projects'), ('codex', self.root/'.codex/state_5.sqlite')]:
            with self.subTest(kind=kind):
                self.store.connections.save(kind, {'enabled':True})
                result=self.store.connections.check(kind)
                self.assertFalse(result['ok']);self.assertEqual(result['status'], 'missing_source')
                self.assertIn(str(expected), result['message']);self.assertFalse(expected.exists())

    def test_codex_connection_requires_both_session_and_history_databases(self):
        source=self.root/'.codex';source.mkdir();(source/'state_5.sqlite').touch()
        self.store.connections.save('codex', {'enabled':True})
        result=self.store.connections.check('codex')
        self.assertFalse(result['ok']);self.assertIn('thread_history_1.sqlite', result['message'])
        (source/'thread_history_1.sqlite').touch()
        self.assertTrue(self.store.connections.check('codex')['ok'])

    def test_saving_codex_reads_existing_databases_without_changing_auth_or_history(self):
        import connections, scan
        source=self.root/'.codex';source.mkdir()
        state=source/'state_5.sqlite';history=source/'thread_history_1.sqlite';now=time.time()
        with contextlib.closing(sqlite3.connect(state)) as db, db:
            db.execute('CREATE TABLE threads(id,updated_at,cwd,title,git_branch,source,created_at,archived)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',('existing-codex',now,str(self.root/'내 작업'),'기존 작업','','cli',now,0))
        with contextlib.closing(sqlite3.connect(history)) as db, db:
            db.execute('CREATE TABLE thread_turns(thread_id,turn_id,status,started_at,completed_at,rollout_ordinal)')
            db.execute('CREATE TABLE thread_items(thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal)')
            db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)',('existing-codex','turn','completed',now,now,1))
            db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?)',('existing-codex','turn','userMessage',json.dumps({'content':[{'text':'기존 작업 요청'}]}),now*1000,1))
        auth=source/'auth.json';auth.write_text('{"existing":"credential fixture"}')
        snapshot={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in (state,history,auth)}
        for path in snapshot:path.chmod(0o400)
        with patch.object(connections,'STORE',self.store.connections),patch.object(scan,'BOARD_DB',self.root/'board.db'), \
                patch.object(scan,'CODEX_STATE',str(state)),patch.object(scan,'CODEX_HISTORY',str(history)):
            self.assertEqual(scan.all_sessions(),[])
            self.store.connections.save('codex',{'enabled':True})
            self.assertEqual([row['id'] for row in scan.all_sessions()],['existing-codex'])
        self.assertEqual({path:(path.read_bytes(),path.stat().st_mtime_ns) for path in snapshot},snapshot)

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
        with patch.object(connections, 'STORE', self.store.connections), \
                patch.object(scan, 'BOARD_DB', self.root / 'board.db'), \
                patch.object(scan, 'claude_sessions', return_value=[]) as claude, \
                patch.object(scan, 'codex_sessions', return_value=[]) as codex:
            scan.all_sessions()
            claude.assert_not_called()
            codex.assert_called_once()

    def test_windows_sessions_do_not_require_posix_delegation_metadata(self):
        import connections, scan

        self.store.connections.save('claude', {'enabled':True});self.store.connections.save('codex', {'enabled':True})
        claude = {'id': 'windows-claude', 'agent': 'claude', 'state': '작업 중', 'last_ts': 2}
        codex = {'id': 'windows-codex', 'agent': 'codex', 'state': '작업 중', 'last_ts': 1}
        with patch.object(connections, 'STORE', self.store.connections), \
                patch.object(scan, 'BOARD_DB', self.root / 'board.db'), \
                patch.object(scan.delegation, 'IS_WINDOWS', True), \
                patch.object(scan, 'claude_sessions', return_value=[claude]), \
                patch.object(scan, 'codex_sessions', return_value=[codex]), \
                patch.object(scan.delegation.os, 'getuid', create=True, side_effect=AssertionError('POSIX uid is unavailable')):
            self.assertEqual(scan.all_sessions(), [claude, codex])

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
