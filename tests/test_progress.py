import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import progress
import scan


class PlanProgressTests(unittest.TestCase):
    def test_reads_actual_checkboxes_and_ignores_code_examples(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            plan=root/'기획/화면/계획서/진행.md'
            plan.parent.mkdir(parents=True)
            plan.write_text('# 작업 계획\n- [x] 원인 확인\n- [~] 구현\n- [ ] 검증\n```md\n- [x] 예제\n```\n')
            result=progress.read_plan(str(plan),str(root))
            self.assertEqual([i['state'] for i in result['items']],['done','active','pending'])
            self.assertEqual(result['rel'],'기획/화면/계획서/진행.md')

    def test_arbitrary_files_and_symlink_escape_are_not_read_as_plans(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'docs';root.mkdir()
            outside=Path(d)/'outside.md';outside.write_text('- [x] old task')
            link=root/'계획서/link.md';link.parent.mkdir();link.symlink_to(outside)
            for path in (outside,link,root/'AGENTS.md'):
                self.assertIsNone(progress.read_plan(str(path),str(root)))

    def test_new_request_does_not_inherit_previous_request_checklist(self):
        first=scan._new_task('이전 작업',1)
        first['plan_refs']=['/known/plan.md']
        scan._close(first,2)
        second=scan._new_task('새로운 작업',3)
        with patch.object(scan.progress,'read_plan',return_value={'items':[{'title':'이전 작업','state':'done'}]}):
            result=scan._finish([first,second],True,4)
            self.assertIsNone(result[-1].get('progress'))

    def test_current_request_uses_a_successfully_written_plan(self):
        info=scan._new_info();scan._open_task(info,'화면 개선',1)
        plan=str(Path.home()/'vtm/문서/화면/개선/계획서/구현.md')
        scan._feed(info,__import__('json').dumps({'type':'assistant','message':{'content':[
            {'type':'tool_use','id':'write','name':'Write','input':{'file_path':plan}}]}}))
        scan._feed(info,__import__('json').dumps({'type':'user','message':{'content':[
            {'type':'tool_result','tool_use_id':'write','content':'ok'}]}}))
        expected={'items':[{'title':'구현','state':'active'}],'rel':'화면/개선/계획서/구현.md','ts':3}
        with patch.object(scan.progress,'read_plan',return_value=expected):
            self.assertEqual(scan._finish(info['tasks'],True,3)[-1].get('progress'),expected)

    def test_reading_an_old_plan_for_reference_does_not_adopt_its_progress(self):
        info=scan._new_info();scan._open_task(info,'새 작업의 조사',100)
        plan=str(Path.home()/'vtm/문서/화면/이전/계획서/과거.md')
        scan._feed(info,__import__('json').dumps({'type':'assistant','message':{'content':[
            {'type':'tool_use','id':'read','name':'Read','input':{'file_path':plan}}]}}))
        with patch.object(scan.progress,'read_plan',return_value={'items':[{'title':'과거','state':'done'}],'ts':1}):
            self.assertIsNone(scan._finish(info['tasks'],True,101)[-1].get('progress'))

    def test_failed_write_does_not_adopt_an_existing_plan(self):
        info=scan._new_info();scan._open_task(info,'새 작업',100)
        plan=str(Path.home()/'vtm/문서/화면/이전/계획서/과거.md')
        for event in ({'type':'assistant','message':{'content':[{'type':'tool_use','id':'write','name':'Write','input':{'file_path':plan}}]}},
                      {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'write','is_error':True,'content':'write failed'}]}}):
            scan._feed(info,__import__('json').dumps(event))
        with patch.object(scan.progress,'read_plan',return_value={'items':[{'title':'과거','state':'done'}],'ts':101}):
            self.assertIsNone(scan._finish(info['tasks'],True,102)[-1].get('progress'))

    def test_parent_and_children_are_not_counted_twice(self):
        items=progress.checklist('- [ ] 구현\n  - [x] 화면\n  - [ ] 서버\n- [x] 조사\n')
        self.assertEqual([i['title'] for i in items],['구현','조사'])


if __name__=='__main__':unittest.main()
