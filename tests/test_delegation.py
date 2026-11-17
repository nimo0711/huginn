import tempfile
import unittest
from pathlib import Path

import delegation


class DelegationTests(unittest.TestCase):
    def test_launch_record_links_child_to_parent_and_the_work_at_launch_time(self):
        with tempfile.TemporaryDirectory() as d:
            parent={'id':'parent-a','agent':'claude','tasks':[{'ts':10},{'ts':30}],
                    'works':[{'key':'old','tasks':[0]},{'key':'current','tasks':[1]}]}
            child={'id':'11111111-2222-3333-4444-555555555555','agent':'codex','created_at':20}
            path=Path(d)/'project/parent-a/tasks/task.output';path.parent.mkdir(parents=True)
            path.write_text('[codex] Starting Codex task thread.\n[codex] Thread ready ('+child['id']+').\n')
            delegation.attach([parent,child],d)
            self.assertEqual(child.get('parent_id'),'parent-a')
            self.assertEqual(child.get('parent_work'),'old')

    def test_same_issue_or_prompt_does_not_imply_a_parent_relationship(self):
        with tempfile.TemporaryDirectory() as d:
            parent={'id':'parent','agent':'claude','tasks':[],'works':[]}
            child={'id':'child','agent':'codex','title':'same review'}
            delegation.attach([parent,child],d)
            self.assertNotIn('parent_id',child)

    def test_orphan_or_ambiguous_launch_record_does_not_hide_child(self):
        with tempfile.TemporaryDirectory() as d:
            child={'id':'11111111-2222-3333-4444-555555555555','agent':'codex'}
            parents=[{'id':p,'agent':'claude','tasks':[],'works':[]} for p in ('one','two')]
            for parent in parents:
                path=Path(d)/('project/'+parent['id']+'/tasks/job.output');path.parent.mkdir(parents=True)
                path.write_text('[codex] Starting Codex task thread.\n[codex] Thread ready ('+child['id']+').\n')
            delegation.attach(parents+[child],d)
            self.assertNotIn('parent_id',child)
            delegation.attach([child],d)
            self.assertNotIn('parent_id',child)


if __name__=='__main__':unittest.main()
