import json
import unittest
from unittest.mock import patch

import enrich
import scan


class UnitTests(unittest.TestCase):
    def resolve(self, key, rows):
        with patch.object(enrich, 'story', side_effect=lambda k: rows.get(k, {})):
            return enrich.work_unit(key, rows.get(key, {}))

    def test_parent_story_from_actual_jira_fields(self):
        fields = {'summary': 'console', 'issuetype': {'name': '하위작업', 'subtask': True},
                  'parent': {'key': 'ABC-1', 'fields': {'summary': 'installation',
                             'issuetype': {'name': '스토리', 'hierarchyLevel': 0}}}}
        with patch.object(enrich, '_run', return_value=json.dumps({'fields': fields})) as run:
            info = enrich._jira('ABC-2')
        self.assertIn('issuetype,parent', run.call_args.args[0][-2])
        unit = self.resolve('ABC-2', {'ABC-2': info})
        self.assertEqual((unit['key'], unit['kind'], unit['title']), ('ABC-1', 'story', 'installation'))
        unit = self.resolve('ABC-2', {'ABC-2': info, 'ABC-1': {'issue_type': None, 'title': ''}})
        self.assertEqual((unit['key'], unit['kind']), ('ABC-1', 'story'))

    def test_task_below_epic_does_not_become_a_story(self):
        unit = self.resolve('P-2', {'P-2': {'title': 'independent', 'issue_type': {'name': 'Task', 'hierarchyLevel': 0},
                              'parent': {'key': 'P-1', 'issue_type': {'name': 'Epic', 'hierarchyLevel': 1}}}})
        self.assertEqual((unit['key'], unit['kind']), ('P-2', 'task'))

    def test_subtask_of_task_uses_task_and_follows_real_story_ancestor(self):
        rows = {'P-3': {'issue_type': {'name': 'Sub-task', 'subtask': True}, 'parent': {'key': 'P-2', 'issue_type': {'name': 'Task'}}},
                'P-2': {'title': 'task', 'issue_type': {'name': 'Task'}}}
        self.assertEqual(self.resolve('P-3', rows)['key'], 'P-2')
        rows['P-2']['parent'] = {'key': 'P-1', 'title': 'story', 'issue_type': {'name': 'Story'}}
        self.assertEqual(self.resolve('P-3', rows)['key'], 'P-1')

    def test_unknown_type_and_cycle_never_invent_a_story(self):
        self.assertEqual(self.resolve('X-1', {})['key'], 'X-1')
        rows = {'X-1': {'issue_type': {'name': 'Task'}, 'parent': {'key': 'X-2'}},
                'X-2': {'parent': {'key': 'X-1'}}}
        self.assertEqual(self.resolve('X-1', rows)['key'], 'X-1')

    def test_refresh_failure_preserves_hierarchy(self):
        old = {'title': 'task', 'issue_type': {'name': 'Task'}, 'parent': {'key': 'P-1'}, 'hierarchy_version': 1}
        with patch.dict(enrich._data, {'P-2': old}), patch.object(enrich, '_jira', return_value={}), \
             patch.object(enrich, '_mrs', return_value=[]), patch.object(enrich, '_commits', return_value=[]), patch.object(enrich, '_save'):
            enrich._refresh('P-2')
            self.assertEqual(enrich._data['P-2']['parent'], old['parent'])

    def test_scanner_attaches_unit_without_rewriting_work_or_parent_link_keys(self):
        task = scan._new_task('change', 1)
        task['branch'] = 'feature/PR-2'
        with patch.dict(scan._jira, {'PR-2': 'task'}), patch.object(enrich, 'story', return_value={'issue_type': {'name': 'Story'}, 'title': 'story'}):
            work = scan._works([task], '', {})[0]
        self.assertEqual(work['key'], 'PR-2')
        self.assertEqual(work['unit']['kind'], 'story')
