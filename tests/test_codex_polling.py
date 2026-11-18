"""Repeated polls must not reparse unchanged turn bodies or hide new activity."""
import json
import os
from pathlib import Path
import sqlite3
import sys
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

import scan
from document_fixtures import document_connection


class CodexPollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.history = self.root / 'history.sqlite'
        self.state = self.root / 'state.sqlite'
        self.addCleanup(patch.stopall)
        patch.object(scan, 'CODEX_HISTORY', str(self.history)).start()
        patch.object(scan, 'CODEX_STATE', str(self.state)).start()
        patch.object(scan.title_summary, 'get', return_value={'title': '', 'state': 'disabled'}).start()
        patch.object(scan.enrich, 'story', return_value={}).start()
        patch.object(scan, '_codex_past', {}).start()
        if hasattr(scan, '_codex_recent'):
            patch.object(scan, '_codex_recent', {}).start()
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('CREATE TABLE threads(id,updated_at,cwd,title,git_branch,source,created_at,archived)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                       ('s', 100, str(self.root), '기능 수정', '', 'cli', 1, 0))
        self.make_history(self.history)
        self.write("INSERT INTO thread_turns VALUES('s','t','inProgress',1,NULL,1)")
        self.add_item('userMessage', {'content': [{'text': '기능 수정'}]}, 1)
        self.add_item('agentMessage', {'text': '분석 중입니다.'}, 2)

    def make_history(self, path, revisions=True):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE thread_turns(thread_id,turn_id,status,started_at,completed_at,rollout_ordinal)')
            db.execute('CREATE TABLE thread_items(thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal' +
                       (',updated_at_ordinal' if revisions else '') + ')')

    def write(self, sql, args=()):
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute(sql, args)

    def add_item(self, kind, body, ordinal, turn='t'):
        self.write('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)',
                   ('s', turn, kind, json.dumps(body), ordinal * 1000, ordinal, ordinal))

    def poll(self):
        return scan.codex_sessions(101)[0]

    def test_unchanged_poll_does_not_read_item_bodies_again(self):
        self.poll()
        queries = []
        connect = sqlite3.connect

        def trace(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(queries.append)
            return db

        with patch.object(scan.sqlite3, 'connect', side_effect=trace):
            result = self.poll()
        self.assertEqual(result['tasks'][0]['now'], '분석 중입니다.')
        bodies = [q for q in queries if q.lstrip().upper().startswith('SELECT') and 'item_json' in q]
        self.assertEqual(bodies, [], 'An unchanged poll reread the entire turn body')

    def test_failed_poll_releases_both_open_databases(self):
        connect = sqlite3.connect
        opened = []

        def record_connection(*args, **kwargs):
            db = connect(*args, **kwargs)
            opened.append(db)
            self.addCleanup(db.close)
            return db

        with patch.object(scan.sqlite3, 'connect', side_effect=record_connection), \
                patch.object(scan, '_finish', side_effect=ValueError('invalid saved turn')):
            with self.assertRaisesRegex(ValueError, 'invalid saved turn'):
                self.poll()

        self.assertEqual(len(opened), 2)
        for db in opened:
            with self.assertRaisesRegex(sqlite3.ProgrammingError, 'closed database'):
                db.execute('SELECT 1')

    def test_in_place_update_and_completion_are_visible_on_next_poll(self):
        self.poll()
        self.write('UPDATE thread_items SET item_json=?,updated_at_ordinal=3 WHERE item_type=?',
                   (json.dumps({'text': '수정을 완료했습니다.'}), 'agentMessage'))
        self.write("UPDATE thread_turns SET status='completed',completed_at=4")
        result = self.poll()
        self.assertEqual(result['state'], '쉬는 중')
        self.assertEqual(result['tasks'][0]['result'], '수정을 완료했습니다.')
        self.assertEqual(result['tasks'][0]['end_ts'], 4)

    def test_running_turn_receives_new_items_and_removes_deleted_items(self):
        self.poll()
        self.add_item('agentMessage', {'text': '검증 중입니다.'}, 3)
        self.assertEqual(self.poll()['tasks'][0]['now'], '검증 중입니다.')
        self.write('DELETE FROM thread_items WHERE rollout_ordinal=3')
        self.assertEqual(self.poll()['tasks'][0]['now'], '분석 중입니다.')

    def test_status_only_update_is_not_hidden_by_unchanged_items(self):
        self.poll()
        self.write("UPDATE thread_turns SET status='interrupted',completed_at=5")
        task = self.poll()['tasks'][0]
        self.assertEqual(task['outcome'], '중단')
        self.assertEqual(task['end_ts'], 5)

    def test_completed_turn_can_still_receive_a_corrected_result(self):
        self.write("UPDATE thread_turns SET status='completed',completed_at=4")
        self.poll()
        self.write('UPDATE thread_items SET item_json=?,updated_at_ordinal=5 WHERE item_type=?',
                   (json.dumps({'text': '추가로 확인한 결과입니다.'}), 'agentMessage'))
        self.assertEqual(self.poll()['tasks'][0]['result'], '추가로 확인한 결과입니다.')

    def test_cached_results_do_not_freeze_plan_files_or_keep_response_mutations(self):
        plan = self.root / '기능/계획서/계획.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('- [ ] 검증\n', encoding='utf-8')
        self.add_item('fileChange', {'status': 'completed', 'changes': [{'path': str(plan)}]}, 3)
        with document_connection(self.root):
            first = self.poll()
            self.assertEqual(first['tasks'][0]['progress']['items'][0]['state'], 'pending')
            first['tasks'][0]['areas']['wrong'] = 1
            first['tasks'][0]['notes'][0]['text'] = '오염된 응답'
            plan.write_text('- [x] 검증\n', encoding='utf-8')
            second = self.poll()
        self.assertEqual(second['tasks'][0]['progress']['items'][0]['state'], 'done')
        self.assertEqual(second['tasks'][0]['now'], '분석 중입니다.')
        self.assertNotIn('wrong', second['tasks'][0]['areas'])

    def test_replaced_database_does_not_reuse_old_turn_with_identical_revision(self):
        self.poll()
        replacement = self.root / 'replacement.sqlite'
        self.make_history(replacement)
        with closing(sqlite3.connect(replacement)) as db, db:
            db.execute("INSERT INTO thread_turns VALUES('s','t','inProgress',1,NULL,1)")
            for ordinal, text in [(1, '교체된 요청'), (2, '교체된 결과')]:
                db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)',
                           ('s', 't', 'agentMessage', json.dumps({'text': text}), ordinal * 1000, ordinal, ordinal))
        os.replace(replacement, self.history)
        self.assertEqual(self.poll()['tasks'][0]['now'], '교체된 결과')

    def test_legacy_database_without_revisions_still_updates(self):
        self.poll()
        replacement = self.root / 'legacy.sqlite'
        self.make_history(replacement, revisions=False)
        with closing(sqlite3.connect(replacement)) as db, db:
            db.execute("INSERT INTO thread_turns VALUES('s','t','inProgress',1,NULL,1)")
            db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?)',
                       ('s', 't', 'agentMessage', json.dumps({'text': '구버전 결과'}), 2000, 2))
        os.replace(replacement, self.history)
        self.assertEqual(self.poll()['tasks'][0]['now'], '구버전 결과')
        self.write('UPDATE thread_items SET item_json=?', (json.dumps({'text': '새 결과'}),))
        self.assertEqual(self.poll()['tasks'][0]['now'], '새 결과')

    def test_recent_cache_is_bounded_and_removed_when_session_leaves(self):
        for ordinal in range(2, 47):
            turn = str(ordinal)
            self.write('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)',
                       ('s', turn, 'completed', ordinal, ordinal + 0.5, ordinal))
            self.add_item('agentMessage', {'text': '결과'}, ordinal + 2, turn)
            self.poll()
        self.assertEqual(sum(len(turns) for turns in scan._codex_recent.values()), scan.TASKS_KEPT)
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute('UPDATE threads SET archived=1')
        self.assertEqual(scan.codex_sessions(101), [])
        self.assertEqual(scan._codex_recent, {})

    def test_empty_thread_drops_its_cache_before_history_is_rebuilt(self):
        self.poll()
        self.write('DELETE FROM thread_items')
        self.write('DELETE FROM thread_turns')
        self.assertEqual(scan.codex_sessions(101), [])
        self.assertEqual(scan._codex_recent, {})
        self.write("INSERT INTO thread_turns VALUES('s','t','inProgress',1,NULL,1)")
        self.add_item('userMessage', {'content': [{'text': '기능 수정'}]}, 1)
        self.add_item('agentMessage', {'text': '복구된 이력'}, 2)
        self.assertEqual(self.poll()['tasks'][0]['now'], '복구된 이력')

    @unittest.skipIf(sys.platform == 'win32', 'requires replacing a database while SQLite holds it open')
    def test_replacement_after_connect_cannot_cache_old_rows_under_new_database(self):
        replacement = self.root / 'replacement.sqlite'
        with closing(sqlite3.connect(self.history)) as old, closing(sqlite3.connect(replacement)) as new, new:
            old.backup(new)
            new.execute('UPDATE thread_items SET item_json=? WHERE item_type=?',
                        (json.dumps({'text': '교체된 결과'}), 'agentMessage'))
        connect = sqlite3.connect

        def replace_after_open(*args, **kwargs):
            db = connect(*args, **kwargs)
            if str(self.history) in str(args[0]):
                # Keep the open handle on the old inode before replacing its path.
                db.execute('SELECT count(*) FROM thread_items').fetchone()
                os.replace(replacement, self.history)
            return db

        with patch.object(scan.sqlite3, 'connect', side_effect=replace_after_open):
            scan.codex_sessions(101)
        self.assertEqual(self.poll()['tasks'][0]['now'], '교체된 결과')


if __name__ == '__main__':
    unittest.main()
