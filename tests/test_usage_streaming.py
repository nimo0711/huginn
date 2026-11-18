"""Large log files must not require a second in-memory copy to count usage."""
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

import usage


class ClaudeStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'session.jsonl'
        patcher = patch.object(usage, '_files', {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def event(self, mid='one', output=1, content=None):
        return {'type': 'assistant', 'timestamp': '2026-11-18T03:00:00Z',
                'message': {'id': mid, 'model': 'test-model', 'usage': {'input_tokens': 10, 'output_tokens': output},
                            'content': content or []}}

    def encode(self, value):
        return json.dumps(value, ensure_ascii=False).encode()

    def scan(self):
        usage._scan_file(str(self.path), self.path.stat().st_size)
        return usage._files[str(self.path)]

    def total(self):
        return sum(self.scan()['days'].values())

    def test_large_file_counts_all_records_without_file_sized_allocations(self):
        # 16MiB on disk; the budget allows many rows, but not a whole-file buffer.
        content = [{'type': 'image', 'source': {'data': 'a' * (128 * 1024)}}]
        with self.path.open('wb') as stream:
            for index in range(128):
                stream.write(self.encode(self.event(str(index), content=content)) + b'\n')
        tracemalloc.start()
        try:
            total = self.total()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(total, 1408)
        self.assertLess(peak, 8 * 1024 * 1024, 'The reader buffers the full log instead of complete records')

    def test_partial_utf8_line_is_deferred_then_counted_once(self):
        first = self.encode(self.event()) + b'\n'
        next_line = self.encode(self.event('two', content=[{'type': 'text', 'text': '진행 중'}]))
        split = next_line.index('진'.encode()) + 1
        self.path.write_bytes(first + next_line[:split])
        self.assertEqual(self.total(), 11)
        self.assertEqual(self.scan()['offset'], len(first))
        with self.path.open('ab') as stream:
            stream.write(next_line[split:] + b'\r\n')
        self.assertEqual(self.total(), 22)
        self.assertEqual(self.total(), 22)
        self.assertEqual(self.scan()['offset'], self.path.stat().st_size)
        self.assertEqual(self.scan()['partial'], set())

    def test_revised_token_record_and_tool_outcome_keep_final_counts(self):
        tool = [{'type': 'tool_use', 'id': 'tool', 'name': 'Bash'}]
        first = self.event(content=tool)
        updated = self.event(output=20, content=tool)
        result = {'type': 'user', 'timestamp': '2026-11-18T03:01:00Z',
                  'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tool',
                                           'is_error': True, 'content': 'exit code 1'}]}}
        self.path.write_bytes(b'\n'.join(self.encode(row) for row in [first, updated, result, result]) + b'\n')
        state = self.scan()
        self.assertEqual(sum(state['days'].values()), 30)
        self.assertEqual(sum(sum(day.values()) for day in state['s']['tools'].values()), 1)
        self.assertEqual(sum(sum(day.values()) for day in state['s']['checked'].values()), 1)
        self.assertEqual(sum(sum(day.values()) for day in state['s']['errors'].values()), 1)

    def test_shorter_replacement_resets_previous_counts_and_skips_malformed_lines(self):
        self.path.write_bytes(self.encode(self.event(content=[{'type': 'text', 'text': 'a' * 1000}])) + b'\n')
        self.assertEqual(self.total(), 11)
        self.path.write_bytes(b'{"assistant": broken}\n' + self.encode(self.event('new', 2)) + b'\n')
        self.assertEqual(self.total(), 12)
        self.assertEqual(self.scan()['partial'], {'malformed'})

    def test_valid_json_with_invalid_field_type_stays_partial_and_keeps_good_rows(self):
        bad = self.event(['invalid-id'], content=[{'type': 'tool_use', 'id': 'tool', 'name': 'Bash'}])
        rows = [self.event('first'), bad, self.event('last', output=4)]
        self.path.write_bytes(b'\n'.join(self.encode(row) for row in rows) + b'\n')
        for _ in range(2):
            state = self.scan()
            self.assertEqual(sum(state['days'].values()), 25)
            self.assertEqual(state['partial'], {'malformed'})
            self.assertEqual(sum(sum(day.values()) for day in state['s']['tools'].values()), 1)


if __name__ == '__main__':
    unittest.main()
