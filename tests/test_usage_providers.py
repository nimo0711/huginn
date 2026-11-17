import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import usage


class ProviderUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude = self.root / 'claude'; self.claude.mkdir()
        self.codex = self.root / 'codex'; self.codex.mkdir()
        self.flags = {'claude': False, 'codex': True}
        self.addCleanup(patch.stopall)
        patch.object(usage, 'PROJECTS', str(self.claude)).start()
        patch.object(usage, 'CODEX_SESSIONS', str(self.codex), create=True).start()
        patch.object(usage, 'CODEX_ARCHIVE', str(self.root / 'archive'), create=True).start()
        patch.object(usage.connections.STORE, 'get', side_effect=lambda name: {'enabled': self.flags.get(name, False)}).start()
        patch.dict(usage._files, {}, clear=True).start()
        patch.object(usage, '_codex_files', {}, create=True).start()
        patch.dict(usage._result, {}, clear=True).start()
        patch.object(usage, '_limit', return_value={}).start()
        self.now = datetime.now().astimezone().replace(microsecond=0)

    def event(self, kind, payload, seconds=0):
        return {'timestamp': (self.now + timedelta(seconds=seconds)).isoformat(), 'type': kind, 'payload': payload}

    def meta(self, sid='one', seconds=-10, **extra):
        return self.event('session_meta', {'id': sid, 'cwd': str(self.root / 'project'), 'timestamp': (self.now + timedelta(seconds=seconds)).isoformat(), 'source': 'cli', **extra}, seconds)

    def tokens(self, inp, out, seconds=0, last=None, cached=0):
        value = {'input_tokens': inp, 'output_tokens': out, 'cached_input_tokens': cached, 'reasoning_output_tokens': out // 2, 'total_tokens': inp + out}
        return self.event('event_msg', {'type': 'token_count', 'info': {'total_token_usage': value, 'last_token_usage': value if last is None else last}}, seconds)

    def item(self, kind, identifier, seconds=0, sid='one', **extra):
        return self.event('event_msg', {'type': 'item_completed', 'thread_id': sid, 'turn_id': 'turn', 'item': {'type': kind, 'id': identifier, **extra}}, seconds)

    def write(self, events, name='one.jsonl'):
        path = self.codex / name
        path.write_text(''.join(json.dumps(e) + '\n' for e in events))
        return path

    def refresh(self):
        usage._refresh()
        self.assertIn('providers', usage._result, 'usage must expose independently collected providers')
        return usage._result

    def test_codex_only_uses_cumulative_deltas_without_double_counting_cache_or_last(self):
        first = self.tokens(100, 20, cached=80)
        self.write([self.meta(), self.event('turn_context', {'model': 'gpt-test'}), first, first,
                    self.tokens(150, 30, 1, last={'input_tokens': 50, 'output_tokens': 10}, cached=100)])
        result = self.refresh()
        self.assertEqual(result['today'], 180)
        self.assertEqual(result['providers']['codex']['today'], 180)
        self.assertTrue(result['providers']['claude']['disabled'])
        self.assertEqual(result['stats']['models'], [{'model': 'gpt-test', 'tokens': 180}])

    def test_claude_and_codex_add_without_changing_claude_token_basis(self):
        self.flags['claude'] = True
        self.claude.joinpath('c.jsonl').write_text(json.dumps({'timestamp': self.now.isoformat(), 'type': 'assistant', 'message': {'id': 'message', 'model': 'claude-test', 'usage': {'input_tokens': 100, 'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 50, 'output_tokens': 30}, 'content': []}}) + '\n')
        self.write([self.meta(), self.tokens(100, 20)])
        result = self.refresh()
        self.assertEqual(result['providers']['claude']['today'], 200)
        self.assertEqual(result['providers']['codex']['today'], 120)
        self.assertEqual(result['today'], 320)

    def test_incremental_read_partial_line_and_truncation(self):
        path = self.write([self.meta(), self.tokens(100, 20)])
        self.assertEqual(self.refresh()['today'], 120)
        encoded = json.dumps(self.tokens(150, 30, 1))
        with path.open('a') as stream: stream.write(encoded[:30])
        self.assertEqual(self.refresh()['today'], 120)
        with path.open('a') as stream: stream.write(encoded[30:] + '\n')
        self.assertEqual(self.refresh()['today'], 180)
        self.assertEqual(self.refresh()['today'], 180)
        self.write([self.meta(), self.tokens(4, 1)])
        self.assertEqual(self.refresh()['today'], 5)

    def test_reset_does_not_lose_new_usage_or_recount_replayed_snapshots(self):
        first = self.tokens(100, 20)
        self.write([self.meta(), first, self.event('compacted', {}, 1), self.tokens(0, 0, 2),
                    self.tokens(10, 2, 3), first])
        self.assertEqual(self.refresh()['today'], 132)

    def test_fork_copied_history_is_baseline_not_child_usage(self):
        parent = self.tokens(100, 20, -5)
        copied = self.item('CommandExecution', 'parent-command', -4, status='completed', exit_code=0)
        self.write([self.meta(), parent, copied])
        self.write([self.meta('child', -1, forked_from_id='one'), parent, copied,
                    self.tokens(110, 22, 1, last={'input_tokens': 10, 'output_tokens': 2})], 'child.jsonl')
        result = self.refresh()
        self.assertEqual(result['today'], 132)
        self.assertEqual(sum(x['count'] for x in result['stats']['tools']), 1)

    def test_initial_inherited_total_counts_only_the_newest_known_increment(self):
        self.write([self.meta('child', parent_thread_id='parent'), self.tokens(1000, 100, last={'input_tokens': 10, 'output_tokens': 2}), self.tokens(1020, 104, 1)])
        result = self.refresh()['providers']['codex']
        self.assertEqual(result['today'], 36)
        self.assertEqual(result['coverage']['tokens'], 'partial')

    def test_structured_tools_have_checked_unknown_and_blocked_denominators(self):
        command = self.item('CommandExecution', 'bad', status='failed', exit_code=1)
        self.write([self.meta(), self.tokens(10, 2), command, command,
                    self.item('CommandExecution', 'blocked', 1, status='declined'),
                    self.item('ImageView', 'image', 2),
                    self.event('response_item', {'type': 'custom_tool_call', 'call_id': 'wrapper', 'name': 'exec'})])
        tools = self.refresh()['stats']['tools']
        self.assertEqual(sum(t['count'] for t in tools), 3)
        self.assertEqual(sum(t['errors'] for t in tools), 1)
        self.assertEqual(sum(t['checked'] for t in tools), 1)
        self.assertEqual(sum(t['unknown'] for t in tools), 1)
        self.assertEqual(sum(t['blocked'] for t in tools), 1)

    def test_legacy_calls_parse_only_explicit_structured_results(self):
        calls = []
        for cid, value in [('a', json.dumps({'exit_code': 1})), ('b', 'The document contains the word error.')]:
            calls.extend([self.event('response_item', {'type': 'function_call', 'call_id': cid, 'name': 'test_tool'}), self.event('response_item', {'type': 'function_call_output', 'call_id': cid, 'output': value})])
        self.write([self.meta(), self.tokens(10, 2), *calls])
        row = self.refresh()['stats']['tools'][0]
        self.assertEqual((row['count'], row['errors'], row['checked'], row['unknown']), (2, 1, 1, 1))

    def test_user_requests_deduplicate_event_versions_and_exclude_subagents(self):
        user = self.item('UserMessage', 'message', client_id='request', content=[{'type': 'text', 'text': '작업 요청'}])
        legacy = self.event('event_msg', {'type': 'user_message', 'client_id': 'request', 'message': '작업 요청'})
        self.write([self.meta(), self.tokens(10, 2), user, user, legacy])
        self.write([self.meta('child', source={'subagent': {'thread_spawn': {'parent_thread_id': 'one'}}}), self.tokens(5, 1), self.item('UserMessage', 'delegated', sid='child', content=[{'type': 'text', 'text': '위임 요청'}])], 'child.jsonl')
        result = self.refresh()
        self.assertEqual(sum(d['prompts'] for d in result['stats']['daily']), 1)
        self.assertEqual(result['today'], 18)

    def test_missing_provider_is_partial_and_disabling_hides_cached_usage(self):
        self.flags['claude'] = True
        self.write([self.meta(), self.tokens(100, 20)])
        result = self.refresh()
        self.assertTrue(result['partial'])
        self.assertTrue(result['providers']['claude']['unavailable'])
        self.flags['codex'] = False
        with patch.object(usage.threading, 'Thread'):
            result = usage.summary()
        self.assertNotIn('today', result)
        self.assertTrue(result['providers']['codex']['disabled'])

    def test_duplicate_rollout_copy_does_not_duplicate_session_tokens(self):
        events = [self.meta(), self.tokens(100, 20)]
        self.write(events); self.write(events, 'copy.jsonl')
        self.assertEqual(self.refresh()['today'], 120)

    def test_midnight_keeps_only_the_new_increment_in_today(self):
        yesterday = self.now.replace(hour=0, minute=0, second=0) - timedelta(seconds=1)
        first = self.tokens(100, 20); first['timestamp'] = yesterday.isoformat()
        meta = self.meta(); meta['payload']['timestamp'] = (yesterday - timedelta(seconds=1)).isoformat()
        self.write([meta, first, self.tokens(150, 30)])
        result = self.refresh()
        self.assertEqual(result['today'], 60)
        self.assertEqual(result['week'], 180)

    def test_revised_same_response_keeps_all_increments_and_duplicate_copies(self):
        def record(inp, out, seconds=0):
            return self.event('token_usage_record', {'response_id': 'response', 'thread_id': 'one', 'thread_token_usage': {'input_tokens': inp, 'output_tokens': out}}, seconds)
        first = [self.meta(), record(100, 20), self.tokens(100, 20)]
        self.write(first, 'a-copy.jsonl')
        self.write([*first, record(120, 25, 1), self.tokens(120, 25, 1)])
        self.assertEqual(self.refresh()['today'], 145)
        self.assertEqual(self.refresh()['today'], 145)

    def test_absent_token_record_is_unavailable_but_explicit_zero_is_known(self):
        self.write([self.meta(), self.event('event_msg', {'type': 'user_message', 'message': '요청', 'client_id': 'request'})])
        result = self.refresh()
        self.assertTrue(result['tokens_unavailable'])
        self.assertEqual(result['providers']['codex']['coverage']['tokens'], 'unavailable')
        self.write([self.meta(), self.tokens(0, 0)])
        usage._codex_files.clear()
        result = self.refresh()
        self.assertFalse(result['tokens_unavailable'])
        self.assertEqual(result['providers']['codex']['coverage']['tokens'], 'complete')

    def test_claude_result_after_midnight_belongs_to_original_call_day(self):
        self.flags = {'claude': True, 'codex': False}
        midnight = self.now.replace(hour=0, minute=0, second=0)
        rows = [{'timestamp': (midnight - timedelta(seconds=1)).isoformat(), 'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'command', 'name': 'Bash'}]}},
                {'timestamp': (midnight + timedelta(seconds=1)).isoformat(), 'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'command', 'is_error': True, 'content': 'nonzero exit'}]}}]
        self.claude.joinpath('midnight.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        days = self.refresh()['stats']['daily']
        self.assertEqual([(d['tools'], d['checked'], d['errors'], d['unknown']) for d in days[-2:]], [(1, 1, 1, 0), (0, 0, 0, 0)])

    def test_malformed_messages_do_not_abort_other_records_or_providers(self):
        self.flags['claude'] = True
        self.write([self.meta(), self.tokens(100, 20), self.event('event_msg', {'type': 'user_message', 'message': {'unexpected': 'object'}})])
        self.claude.joinpath('malformed.jsonl').write_text(json.dumps({'timestamp': self.now.isoformat(), 'type': 'user', 'message': ['bad']}) + '\n')
        result = self.refresh()
        self.assertEqual(result['today'], 120)
        self.assertTrue(result['partial'])
        self.assertFalse(result.get('stale'))

    def test_active_file_without_tokens_makes_known_provider_total_partial(self):
        self.write([self.meta(), self.tokens(100, 20)])
        self.write([self.meta('empty')], 'empty.jsonl')
        self.assertFalse(self.refresh()['partial'])
        self.write([self.meta('active'), self.event('event_msg', {'type': 'user_message', 'message': '요청', 'client_id': 'request'})], 'active.jsonl')
        result = self.refresh()
        self.assertEqual(result['today'], 120)
        self.assertTrue(result['partial'])
        self.assertEqual(result['coverage']['tokens'], 'partial')
        self.assertFalse(result['tokens_unavailable'])

    def test_copied_tool_records_keep_latest_confirmed_result_once(self):
        call = self.event('response_item', {'type': 'function_call', 'call_id': 'tool', 'name': 'test_tool'})
        def result(code, seconds):
            return self.event('response_item', {'type': 'function_call_output', 'call_id': 'tool', 'output': json.dumps({'exit_code': code})}, seconds)
        for old, new, reverse in [([], [result(1, 2)], False), ([], [result(1, 2)], True),
                                  ([result(0, 1)], [result(1, 2)], False), ([result(0, 1)], [result(1, 2)], True)]:
            with self.subTest(previous_result=bool(old), reverse_paths=reverse):
                usage._codex_files.clear()
                first, second = ('z-new-live.jsonl', 'a-old-copy.jsonl') if reverse else ('a-old-copy.jsonl', 'z-new-live.jsonl')
                self.write([self.meta(), self.tokens(100, 20), call, *old], first)
                self.write([self.meta(), self.tokens(100, 20), call, *new], second)
                for _ in range(2):
                    row = self.refresh()['stats']['tools'][0]
                    self.assertEqual((row['count'], row['checked'], row['errors'], row['unknown']), (1, 1, 1, 0))


if __name__ == '__main__': unittest.main()
