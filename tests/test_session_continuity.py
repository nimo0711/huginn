"""Whole-work history must survive the bounded window of detailed requests."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import scan


class SessionContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(patch.stopall)
        patch.object(scan.title_summary, 'get', return_value={'title': '테스트 업무', 'state': 'done'}).start()
        patch.object(scan, '_jira_title', return_value='Jira 업무').start()
        patch.object(scan.enrich, 'story', return_value={}).start()

    def history(self, count, branch='feature/MP-9876'):
        info = scan._new_info()
        info['branch'] = branch
        for i in range(count):
            scan._open_task(info, '같은 업무의 요청 ' + str(i), i + 1)
            task = info['tasks'][-1]
            task['branch'] = branch
            if i == 0:
                task.update(n_steps=3, start_ts=1)
            scan._note(task, i + 1.5, '검토 결과입니다. ' * 50)
        return info

    def test_codex_file_change_stays_in_the_explicit_jira_branch(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE thread_items(thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal)')
        db.executemany('INSERT INTO thread_items VALUES(?,?,?,?,?,?)', [
            ('s', 't', 'userMessage', json.dumps({'content': [{'text': '기능 구현'}]}), 1000, 1),
            ('s', 't', 'fileChange', json.dumps({'status': 'completed', 'changes': [{'path': str(Path.home()/'vtm/demo-repo/src/example.py')}]}), 2000, 2),
        ])
        task = scan._codex_turn(db, 's', 't', 'inProgress', 1, 0)
        work = scan._works([task], 'feature/MP-9876', {})[0]
        self.assertEqual((work['key'], work['story']), ('MP-9876', 'MP-9876'))

    def test_followup_preserves_the_earlier_unfinished_plan(self):
        plan = self.root / '기능/계획서/계획.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('- [x] 구현\n- [ ] 검증\n')
        first = scan._new_task('구현', 1)
        first['plan_refs'] = [str(plan)]
        scan._note(first, 2, '검증이 남아 있습니다.')
        scan._close(first, 2)
        second = scan._new_task('상태만 알려 주세요.', 3)
        scan._note(second, 4, '검증이 남아 있습니다.')
        scan._close(second, 4)
        with patch.object(scan, 'DOC_ROOT', str(self.root)):
            tasks = scan._finish([first, second], False, 4)
        self.assertIsNotNone(tasks[0]['progress'])
        self.assertEqual(tasks[0]['progress']['items'][-1]['state'], 'pending')

    def test_older_work_is_summarized_without_losing_changes_or_total(self):
        info = self.history(141)
        self.assertEqual(sum(t.get('history', {}).get('total', 1 + len(t.get('extra', []))) for t in info['tasks']), 141)
        self.assertEqual(sum(t['n_steps'] for t in info['tasks']), 3)
        self.assertLessEqual(sum(not t.get('history') for t in info['tasks']), 40)
        self.assertLessEqual(len(info['tasks']), 41)
        old = [t for t in info['tasks'] if t.get('history')]
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]['notes'], [])
        self.assertLess(len(json.dumps(old, ensure_ascii=False)), 2000)

    def test_summaries_do_not_merge_different_jira_works(self):
        info = self.history(45, 'feature/MP-1')
        info['branch'] = 'feature/MP-2'
        for i in range(45, 90):
            scan._open_task(info, '다른 업무', i + 1)
            info['tasks'][-1]['branch'] = info['branch']
            scan._note(info['tasks'][-1], i + 1.5, '결과')
        works = scan._works(info['tasks'], info['branch'], {})
        totals = {w['key']: sum(info['tasks'][i].get('history', {}).get('total', 1) for i in w['tasks']) for w in works}
        self.assertEqual(totals, {'MP-1': 45, 'MP-2': 45})

    def test_archived_pending_plan_blocks_completion_until_checked(self):
        plan = self.root / '기능/계획서/계획.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('- [ ] 검증\n')
        with patch.object(scan, 'DOC_ROOT', str(self.root)):
            info = scan._new_info()
            for i in range(45):
                scan._open_task(info, '업무', i + 1)
                info['tasks'][-1]['branch'] = 'feature/MP-1'
                if i == 0:
                    info['tasks'][-1]['plan_refs'] = [str(plan)]
                scan._note(info['tasks'][-1], i + 1.5, '결과')
            before = scan._finish(info['tasks'], True, 46)
            archived = next((t for t in before if t.get('history')), None)
            self.assertIsNotNone(archived)
            self.assertEqual(archived['history']['total'] - archived['history']['done'], 1)
            plan.write_text('- [x] 검증\n')
            after = scan._finish(info['tasks'], True, 47)
            archived = next(t for t in after if t.get('history'))
            self.assertEqual(archived['history']['total'], archived['history']['done'])

    def test_followup_after_interleaved_nonjira_work_keeps_its_latest_work(self):
        info = scan._new_info()
        for i in range(43):
            scan._open_task(info, '업무', i + 1)
            if i < 3:
                info['tasks'][-1]['areas'] = {['repo-a', 'repo-b', 'repo-a'][i]: 1}
            scan._note(info['tasks'][-1], i + 1.5, '결과')
        works = scan._works(info['tasks'], '', {})
        totals = {w['key']: sum(info['tasks'][i].get('history', {}).get('total', 1) for i in w['tasks']) for w in works}
        self.assertEqual(totals, {'repo-a': 42, 'repo-b': 1})

    def test_codex_old_turns_are_compacted_once_then_only_recent_turns_reparse(self):
        state = self.root / 'state.sqlite'
        history = self.root / 'history.sqlite'
        with sqlite3.connect(state) as db:
            db.execute('CREATE TABLE threads(id,updated_at,cwd,title,git_branch,source,created_at,archived)')
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)', ('fake-thread', 100, str(Path.home()/'vtm/repo'), '테스트', 'feature/MP-9876', 'cli', 1, 0))
        db.close()
        with sqlite3.connect(history) as db:
            db.execute('CREATE TABLE thread_turns(thread_id,turn_id,status,started_at,completed_at,rollout_ordinal)')
            db.execute('CREATE TABLE thread_items(thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal)')
            for i in range(45):
                db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?)', ('fake-thread', str(i), 'completed', i + 1, i + 1.5, i))
                db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?)', ('fake-thread', str(i), 'userMessage', json.dumps({'content': [{'text': '요청'}]}), (i + 1) * 1000, i * 3))
                db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?)', ('fake-thread', str(i), 'agentMessage', json.dumps({'text': '결과'}), (i + 1.5) * 1000, i * 3 + 2))
                if i == 0:
                    db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?)', ('fake-thread', str(i), 'fileChange', json.dumps({'status': 'completed', 'changes': [{'path': str(Path.home()/'vtm/repo/a.py')}]}), 1200, 1))
        db.close()
        with patch.object(scan, 'CODEX_STATE', str(state)), patch.object(scan, 'CODEX_HISTORY', str(history)), patch.object(scan, '_codex_turn', wraps=scan._codex_turn) as parse:
            first = scan.codex_sessions(101)[0]
            first_calls = parse.call_count
            second = scan.codex_sessions(101)[0]
            self.assertEqual(sum(t.get('history', {}).get('total', 1) for t in first['tasks']), 45)
            self.assertEqual(sum(t['n_steps'] for t in first['tasks']), 1)
            self.assertEqual(first_calls, 45)
            self.assertEqual(parse.call_count - first_calls, 40)
            self.assertEqual(first['tasks'], second['tasks'])


if __name__ == '__main__':
    unittest.main()
