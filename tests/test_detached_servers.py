"""Detached server identity comes from its current output, never a reused port."""
import os
import unittest
from unittest.mock import patch
import local_runtime as runtime

A = '00000000-0000-0000-0000-000000000001'
B = '00000000-0000-0000-0000-000000000002'


class DetachedServers(unittest.TestCase):
    def output(self, sid=A, pid=11, fd=1, access='w', uid=None, path=None):
        uid = os.getuid() if uid is None else uid
        path = path or f'/private/tmp/claude-{uid}/-Users-example-project/{sid}/scratchpad/server.log'
        return f'p{pid}\nu{uid}\nf{fd}\na{access}\ntREG\nn{path}\n'

    def refresh(self, output, procs=None, ports=None, ready=True):
        procs = procs or {11: {'ppid': 1, 'comm': 'node'}}
        ports = [{'pid': 11, 'port': 4321}] if ports is None else ports
        sessions = [{'id': A, 'agent': 'claude'}, {'id': B, 'agent': 'claude'}]
        with patch.object(runtime, 'processes', return_value=procs), \
             patch.object(runtime, 'listeners', return_value=ports), \
             patch.object(runtime, 'run', return_value=output), \
             patch.object(runtime, 'codex_roots', return_value={}), \
             patch.object(runtime, 'codex_terminals', return_value={}), \
             patch.object(runtime, 'http_ready', return_value=ready):
            runtime.refresh(sessions)
        return runtime._cache

    def test_detached_http_server_uses_current_writable_session_log(self):
        found = self.refresh(self.output())
        self.assertEqual([s['port'] for s in found[A]['servers']], [4321])
        self.assertEqual(found[B]['servers'], [])

    def test_reload_parent_and_child_show_one_link(self):
        procs = {11: {'ppid': 1, 'comm': 'python'}, 12: {'ppid': 11, 'comm': 'python'}}
        ports = [{'pid': 11, 'port': 4321}, {'pid': 12, 'port': 4321}]
        found = self.refresh(self.output() + self.output(pid=12), procs, ports)
        self.assertEqual(len(found[A]['servers']), 1)

    def test_ordinary_project_path_and_unknown_session_never_bind(self):
        for path in [f'/Users/example/{A}/server.log',
                     f'/private/tmp/unrelated/{A}/scratchpad/server.log',
                     f'/private/tmp/claude-{os.getuid()}/project/{A}/elsewhere/server.log',
                     f'/private/tmp/claude-{os.getuid()}/project/{A}-copy/scratchpad/server.log']:
            with self.subTest(path=path):
                self.assertEqual(self.refresh(self.output(path=path))[A]['servers'], [])
        self.assertEqual(self.refresh(self.output(sid='unknown'))[A]['servers'], [])

    def test_read_only_or_unrelated_descriptor_does_not_prove_launch(self):
        for data in [self.output(access='r'), self.output(fd=3)]:
            self.assertEqual(self.refresh(data)[A]['servers'], [])

    def test_other_users_process_or_temp_directory_is_not_ours(self):
        self.assertEqual(self.refresh(self.output(uid=os.getuid()+1))[A]['servers'], [])
        path = f'/private/tmp/claude-{os.getuid()+1}/project/{A}/scratchpad/server.log'
        self.assertEqual(self.refresh(self.output(path=path))[A]['servers'], [])

    def test_conflicting_output_sessions_are_not_guessed(self):
        found = self.refresh(self.output() + self.output(sid=B, fd=2))
        self.assertEqual(found[A]['servers'], [])
        self.assertEqual(found[B]['servers'], [])

    def test_unscanned_session_output_still_counts_as_conflicting_evidence(self):
        unknown = '00000000-0000-0000-0000-000000000003'
        found = self.refresh(self.output() + self.output(sid=unknown, fd=2))
        self.assertEqual(found[A]['servers'], [])

    def test_live_ancestor_identity_wins_over_output_path(self):
        procs = {10: {'ppid': 1, 'comm': 'claude'}, 11: {'ppid': 10, 'comm': 'node'}}
        with patch.object(runtime, 'http_ready', return_value=True):
            found = runtime.resolve([{'id': A}, {'id': B}], procs,
                                    [{'pid': 11, 'port': 4321}], {10: B}, {11: A})
        self.assertEqual(found[A]['servers'], [])
        self.assertEqual(len(found[B]['servers']), 1)

    def test_new_process_reusing_old_port_does_not_inherit_link(self):
        self.assertEqual(len(self.refresh(self.output())[A]['servers']), 1)
        found = self.refresh('', {12: {'ppid': 1, 'comm': 'node'}}, [{'pid': 12, 'port': 4321}])
        self.assertEqual(found[A]['servers'], [])
        self.assertEqual(self.refresh(self.output(), ports=[])[A]['servers'], [])

    def test_non_http_listener_stays_hidden(self):
        self.assertEqual(self.refresh(self.output(), ready=False)[A]['servers'], [])

    def test_tmp_alias_and_read_write_output_are_supported(self):
        path = f'/tmp/claude-{os.getuid()}/project/{A}/scratchpad/server.log'
        self.assertEqual(len(self.refresh(self.output(path=path, access='u'))[A]['servers']), 1)

    def test_internal_agent_listener_is_still_excluded(self):
        found = self.refresh(self.output(), {11: {'ppid': 1, 'comm': 'claude'}})
        self.assertEqual(found[A]['servers'], [])

    def test_unlisted_process_and_traversal_path_do_not_claim_ownership(self):
        self.assertEqual(self.refresh(self.output(pid=99))[A]['servers'], [])
        path = f'/private/tmp/claude-{os.getuid()}/project/{A}/scratchpad/../../other.log'
        self.assertEqual(self.refresh(self.output(path=path))[A]['servers'], [])


if __name__ == '__main__':
    unittest.main()
