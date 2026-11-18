import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import board_actions
import connections
import progress
import scan
import server


class DocumentConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.home=Path(temporary.name);self.root=self.home/'내 산출물'
        self.plan=self.root/'기능/계획서/진행.md';self.plan.parent.mkdir(parents=True)
        self.plan.write_text('- [x] 구현\n- [ ] 검증\n',encoding='utf-8')
        self.store=board_actions.ActionStore(self.home/'state',self.home)
        self.addCleanup(self.store.delivery.close)
        for owner,name,value in [(connections,'STORE',self.store.connections),(server,'ACTIONS',self.store)]:
            patcher=patch.object(owner,name,value);patcher.start();self.addCleanup(patcher.stop)

    def request(self,relative):
        handler=object.__new__(server.Handler)
        handler._json=lambda status,body:setattr(handler,'result',(status,body))
        handler._send=lambda status,body,kind:setattr(handler,'result',(status,body))
        handler._doc(relative)
        return handler.result

    def register(self,root=None):
        self.store.connections.save('documents',{'enabled':True,'root':str(root or self.root)})

    def test_only_registered_documents_are_read_and_served(self):
        self.assertIsNone(progress.read_plan(str(self.plan)))
        self.assertEqual(self.request('기능/계획서/진행.md')[0],404)
        task=scan._new_task('작업',1);scan._doc(task,str(self.plan),2,True)
        self.assertEqual(task['docs'],[])
        self.register()
        self.assertEqual(progress.read_plan(str(self.plan))['items'][-1]['state'],'pending')
        scan._doc(task,str(self.plan),2,True)
        self.assertEqual(task['docs'][0]['rel'],'기능/계획서/진행.md')
        self.assertEqual(self.request('기능/계획서/진행.md'),(200,self.plan.read_bytes()))
        self.store.connections.save('documents',{'enabled':False})
        self.assertIsNone(progress.read_plan(str(self.plan)))
        self.assertEqual(self.request('기능/계획서/진행.md')[0],404)
        scan._live_docs([task]);self.assertEqual(task['docs'],[])

    def test_document_endpoint_rejects_absolute_parent_and_symlink_escapes(self):
        self.register()
        outside=self.home/'private.md';outside.write_text('not a document')
        for path in [str(self.plan),'../private.md','기능/../../private.md']:
            with self.subTest(path=path):self.assertEqual(self.request(path)[0],404)
        link=self.root/'기능/계획서/link.md'
        try:link.symlink_to(outside)
        except OSError as error:
            if getattr(error,'winerror',None)==1314:self.skipTest('Windows symlink privilege unavailable')
            raise
        self.assertIsNone(progress.read_plan(str(link)))
        self.assertEqual(self.request('기능/계획서/link.md')[0],404)
        task=scan._new_task('작업',1);scan._doc(task,str(link),2,True)
        self.assertEqual(task['docs'],[])

    def test_folder_change_refreshes_existing_session_document_cache(self):
        projects=self.home/'.claude/projects/example';projects.mkdir(parents=True)
        source=projects/'fixture.jsonl'
        rows=[{'type':'user','message':{'content':'계획대로 진행해 주세요.'}},
              {'type':'assistant','message':{'content':[{'type':'tool_use','id':'write','name':'Write','input':{'file_path':str(self.plan)}}]}},
              {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'write','content':'ok'}]}}]
        source.write_text('\n'.join(json.dumps(row) for row in rows)+'\n')
        with patch.object(scan,'CLAUDE_PROJECTS',str(projects.parent)),patch.object(scan,'CLAUDE_SESSIONS',str(self.home/'.claude/sessions')):
            self.assertEqual(scan.claude_sessions(time.time())[0]['tasks'][0]['docs'],[])
            self.register()
            task=scan.claude_sessions(time.time())[0]['tasks'][0]
            self.assertEqual(len(task['docs']),1);self.assertIsNotNone(task['progress'])
            other=self.home/'다른 산출물';other.mkdir();self.register(other)
            task=scan.claude_sessions(time.time())[0]['tasks'][0]
            self.assertEqual(task['docs'],[]);self.assertIsNone(task['progress'])
        self.assertEqual(self.request('기능/계획서/진행.md')[0],404)

    def test_document_folder_is_required_and_must_exist(self):
        for values in [{'enabled':True},{'enabled':True,'root':str(self.home/'missing')}]:
            with self.subTest(values=values),self.assertRaises(ValueError):self.store.connections.save('documents',values)


if __name__=='__main__':unittest.main()
