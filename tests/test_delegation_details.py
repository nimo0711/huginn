import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from unittest.mock import patch

import delegation
import scan


class DelegationDetailTests(unittest.TestCase):
    def test_malformed_metadata_and_collab_fields_do_not_break_collection(self):
        task = scan._new_task('검수', 1)
        for receivers, ident in [(1, 'a'), (['child'], None), (['child', []], 'a')]:
            delegation.codex_call(task, {'tool': 'spawnAgent', 'prompt': '검수', 'id': ident, 'receiverThreadIds': receivers}, 2)
        self.assertEqual(len(task['delegations']), 1)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'subagents';folder.mkdir()
            (folder / 'agent.meta.json').write_text('{"toolUseId":[]}')
            self.assertEqual(len(delegation.claude_children(directory, [task], 100)['items']), 1)

    def test_malformed_record_fields_are_skipped_and_followup_reopens_child(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'child.jsonl'
            rows = [{'type': 'assistant', 'message': []},
                    {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': None},
                     {'type': 'tool_use', 'input': [], 'id': []}]}},
                    {'type': 'user', 'message': {'content': '첫 지시'}},
                    {'type': 'assistant', 'message': {'content': '첫 결과', 'stop_reason': 'end_turn'}}]
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            self.assertEqual(delegation.read_child(path)['result'], '첫 결과')
            with path.open('a') as f:
                f.write(json.dumps({'type': 'user', 'message': {'content': '추가 지시'}})+'\n')
            row = delegation.read_child(path)
            self.assertEqual(row['status'], 'running')
            self.assertEqual(row['result'], '')

    def test_codex_child_keeps_raw_result_and_does_not_queue_title_summary(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE thread_items (thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal)')
            for i, (kind, body) in enumerate([('userMessage', {'content': [{'text': '코드 검수'}]}),
                                             ('agentMessage', {'text': '코드:\n<div>value</div>'})]):
                db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)', ('child', 'turn', kind, json.dumps(body), 1000+i, i))
            task = scan._codex_turn(db, 'child', 'turn', 'completed', 1, 2)
            self.assertEqual(scan._finish([task], False, 3)[0]['raw_result'], '코드:\n<div>value</div>')
            with patch.object(scan.title_summary, 'get') as model:
                scan._works([task], '', {}, delegated=True)
                model.assert_not_called()

    def test_codex_child_limit_does_not_hide_roots_and_includes_older_parent(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('CREATE TABLE threads (id,updated_at,cwd,title,git_branch,source,created_at,archived)')
            for i in range(60):
                source = json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': 'parent'}}})
                db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', (str(i), 99999-i, '', '', '', source, 90000, 0))
            for ident, updated in [('parent', 1), ('root', 99900)]:
                db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)', (ident, updated, '', '', '', 'cli', 1, 0))
            ids = {row[0] for row in scan._codex_session_rows(db, 100000)}
            self.assertTrue({'parent', 'root'}.issubset(ids))

    def test_parallel_calls_keep_their_own_instruction_and_result(self):
        info = scan._new_info()
        scan._open_task(info, '전체 작업', 1)
        for ident, prompt in [('one', '서버 검토\n수정하지 마세요.'), ('two', '화면 수정')]:
            scan._feed(info, json.dumps({'type': 'assistant', 'message': {'content': [{
                'type': 'tool_use', 'id': ident, 'name': 'Agent',
                'input': {'description': ident, 'prompt': prompt}}]}}))
        scan._feed(info, json.dumps({'type': 'user', 'message': {'content': [{
            'type': 'tool_result', 'tool_use_id': 'two', 'content': '화면 수정 완료'}]}}))
        rows = info['tasks'][0].get('delegations', [])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['prompt'], '서버 검토\n수정하지 마세요.')
        self.assertEqual(rows[0]['result'], '')
        self.assertEqual(rows[1]['result'], '화면 수정 완료')

    def test_background_launch_acknowledgement_is_not_a_completed_result(self):
        task = scan._new_task('검수', 1)
        delegation.record_call(task, {'id': 'a', 'name': 'Agent', 'input': {
            'prompt': '검수해 주세요', 'run_in_background': True}}, 2)
        delegation.record_result([task], 'a', 'Agent launched successfully.', 3,
                                 metadata={'agentId': 'child', 'isAsync': True})
        row = task['delegations'][0]
        self.assertEqual(row['child_id'], 'child')
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['result'], '')

    def test_native_record_preserves_completed_result_and_long_idle_is_not_done(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'parent'
            folder = base / 'subagents'
            folder.mkdir(parents=True)
            (folder / 'agent-a.meta.json').write_text(json.dumps({'toolUseId': 'a', 'description': '검수'}))
            log = folder / 'agent-a.jsonl'
            events = [
                {'type': 'user', 'isSidechain': True, 'timestamp': '2026-01-01T00:00:00Z', 'message': {'content': '원래 지시'}},
                {'type': 'assistant', 'timestamp': '2026-01-01T00:00:10Z', 'message': {'content': [{'type': 'text', 'text': '확인 중입니다.'}]}},
            ]
            log.write_text(''.join(json.dumps(e)+'\n' for e in events))
            row = delegation.claude_children(str(base), [], 2000000000)['items'][0]
            self.assertEqual(row['status'], 'unknown')
            self.assertEqual(row['result'], '')
            with log.open('a') as f:
                f.write(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:20Z',
                    'message': {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '문제 두 건을 확인했습니다.'}]}})+'\n')
            row = delegation.claude_children(str(base), [], 2000000000)['items'][0]
            self.assertEqual(row['result'], '문제 두 건을 확인했습니다.')
            self.assertEqual(row['status'], 'completed')
            self.assertEqual(row['end_ts']-row['ts'], 20)

    def test_native_codex_source_links_only_the_explicit_parent(self):
        source = json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': 'parent', 'agent_nickname': 'Sagan'}}})
        self.assertEqual(delegation.codex_parent(source), 'parent')
        self.assertEqual(delegation.codex_parent('exec'), '')
        self.assertEqual(delegation.codex_parent('{invalid'), '')
        parent = {'id': 'parent', 'agent': 'codex', 'tasks': [{'ts': 10}, {'ts': 30}],
                  'works': [{'key': 'old', 'tasks': [0]}, {'key': 'new', 'tasks': [1]}]}
        child = {'id': 'child', 'agent': 'codex', 'source_parent': 'parent', 'created_at': 20,
                 'tasks': [{'instruction': '원본 지시\n제약 유지', 'ts': 20}]}
        delegation.attach([parent, child])
        self.assertEqual(child.get('parent_id'), 'parent')
        self.assertEqual(child.get('parent_work'), 'old')
        self.assertEqual(child.get('handoff', {}).get('prompt'), '원본 지시\n제약 유지')

    def test_truncated_child_log_is_not_lost_and_is_read_incrementally(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'child.jsonl'
            path.write_text('{broken}\n'+json.dumps({'type': 'user', 'message': {'content': '지시'}})+'\n'+
                            '{"type":"assistant","message":{"content":"결과"')
            self.assertEqual(delegation.read_child(path)['prompt'], '지시')
            with path.open('a') as f:
                f.write(',"stop_reason":"end_turn"}}\n')
            self.assertEqual(delegation.read_child(path)['result'], '결과')


if __name__ == '__main__':
    unittest.main()
