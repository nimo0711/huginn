import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import title_summary
import connections


class TitleTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        settings=connections.Connections(Path(temporary.name)/'state',Path(temporary.name))
        settings.save('summary', {'enabled':True})
        patcher=patch.object(connections,'STORE',settings);patcher.start();self.addCleanup(patcher.stop)

    def wait_for(self,cache,context,state='ready'):
        until=time.monotonic()+2
        while time.monotonic()<until:
            result=cache.get(context)
            if result['state']==state:
                return result
            time.sleep(.01)
        self.fail('title job did not finish')

    def test_repeated_polling_and_restart_reuse_saved_title(self):
        calls=[]
        def runner(context):
            calls.append(context)
            return '콘솔 문자 표시 조사'
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'titles.json'
            cache=title_summary.TitleCache(file,runner)
            context={'area':'repo','requests':['콘솔 문자가 안 나옵니다. 원인을 조사해 주세요.']}
            self.assertEqual(self.wait_for(cache,context)['title'],'콘솔 문자 표시 조사')
            for _ in range(20):
                self.assertEqual(cache.get(context)['title'],'콘솔 문자 표시 조사')
            restored=title_summary.TitleCache(file,runner)
            self.assertEqual(restored.get(context)['title'],'콘솔 문자 표시 조사')
            self.assertEqual(len(calls),1)

    def test_changed_user_request_is_summarized_once(self):
        calls=[]
        def runner(c):
            calls.append(c)
            return '업무 목적 요약'
        with tempfile.TemporaryDirectory() as d:
            cache=title_summary.TitleCache(Path(d)/'titles.json',runner)
            self.wait_for(cache,{'requests':['첫 번째 작업']})
            self.wait_for(cache,{'requests':['첫 번째 작업','새로운 작업 요청']})
            self.assertEqual(len(calls),2)

    def test_invalid_title_never_leaks_prompt_and_failure_is_not_retried_each_poll(self):
        for invalid in ('긴 문장을 그대로 잘라 붙인 요청입니다. '*10,'너무 긴 제목…','두 줄\n제목'):
            with self.subTest(invalid=invalid),tempfile.TemporaryDirectory() as d:
                calls=[]
                def runner(c):
                    calls.append(c)
                    return invalid
                cache=title_summary.TitleCache(Path(d)/'titles.json',runner)
                context={'requests':['사용자의 긴 요청 원문']}
                result=self.wait_for(cache,context,'error')
                self.assertEqual(result['title'],'')
                for _ in range(5):cache.get(context)
                self.assertEqual(len(calls),1)

    def test_model_process_has_no_tools_hooks_or_saved_session(self):
        output={'is_error':False,'structured_output':{'title':'배포 실패 원인 조사'}}
        with patch.object(title_summary.subprocess,'run') as run:
            run.return_value.returncode=0
            run.return_value.stdout=json.dumps(output)
            result=title_summary.generate({'requests':['배포가 왜 실패하는지 조사해 주세요.']})
            self.assertEqual(result,'배포 실패 원인 조사')
            args=run.call_args.args[0]
            self.assertIn('--safe-mode',args)
            self.assertIn('--no-session-persistence',args)
            self.assertEqual(args[args.index('--tools')+1],'')
            self.assertIn('--strict-mcp-config',args)
            self.assertLessEqual(run.call_args.kwargs['timeout'],45)

    def test_malformed_model_response_is_rejected(self):
        for output in ([], {'structured_output': 'bad'}, {'is_error': True},
                       {'structured_output': {'title': 'too long ' * 10}}):
            with self.subTest(output=output), patch.object(title_summary.subprocess, 'run') as run:
                run.return_value.returncode=0
                run.return_value.stdout=json.dumps(output)
                with self.assertRaises(ValueError):
                    title_summary.generate({'requests':['배포 실패 조사']})

    def test_issue_number_from_an_example_cannot_become_the_work_identity(self):
        with patch.object(title_summary.subprocess,'run') as run:
            run.return_value.returncode=0
            run.return_value.stdout=json.dumps({'structured_output':{'title':'MP-9999 백업 작업'}})
            with self.assertRaises(ValueError):
                title_summary.generate({'area':'dashboard','objective':'대시보드 수정','turns':[]})


if __name__=='__main__':unittest.main()
