import os
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scan
import enrich
from document_fixtures import document_connection


class QuestionTests(unittest.TestCase):
    def test_heading_question_is_still_an_actual_request(self):
        self.assertEqual(scan._question('### 어느 환경에 적용할까요?'), '어느 환경에 적용할까요?')
    def test_links_code_quotes_and_completed_approvals_do_not_need_response(self):
        examples = [
            '링크: https://example.com/report?usp=sharing',
            '[보고서](https://example.com/?view=1)',
            '승인을 받아 반영했습니다.',
            '허락 없이 파일을 수정하지 않았습니다.',
            '> 이 계획으로 진행할까요?',
            '```js\nconst value = ready ? yes : no;\n```',
            '명령 예시: `curl https://example.com/?q=1`',
        ]
        for value in examples:
            with self.subTest(value=value):
                self.assertEqual(scan._question(value), '')

    def test_actual_requests_survive_with_links(self):
        for value in ['이 계획으로 진행할까요?', '대상 환경을 알려 주세요.',
                      '이 작업은 승인이 필요합니다.', 'Can you confirm the target?',
                      'https://example.com/?q=1 에 반영해도 될까요?']:
            with self.subTest(value=value):
                self.assertTrue(scan._question(value))

    def test_runtime_permission_takes_precedence_over_old_question(self):
        task = scan._new_task('설치', 1)
        task['ask'] = '이전 질문인가요?'
        wait = scan._wait_reason({'status': 'waiting', 'waitingFor': 'approve npm install'}, task, None, False)
        self.assertEqual(wait['kind'], 'perm')
        self.assertEqual(wait['text'], 'npm install')

    def test_answered_explicit_question_is_cleared(self):
        info = scan._new_info()
        scan._open_task(info, '작업 요청', 1)
        scan._feed(info, json.dumps({'type': 'assistant', 'message': {'content': [{
            'type': 'tool_use', 'id': 'q1', 'name': 'AskUserQuestion',
            'input': {'questions': [{'question': '어느 환경인가요?', 'options': [{'label': '개발'}]}]}
        }]}}))
        self.assertTrue(info['tasks'][-1]['ask'])
        scan._feed(info, json.dumps({'type': 'user', 'message': {'content': [{
            'type': 'tool_result', 'tool_use_id': 'q1', 'content': '개발'
        }]}}))
        self.assertEqual(info['tasks'][-1]['ask'], '')
        self.assertEqual(info['tasks'][-1]['options'], [])


class ActivityTests(unittest.TestCase):
    def test_user_request_after_pasted_table_is_preserved(self):
        text = '어려움 실제 상황\n' + ('1. 이전 설명이 길게 이어집니다.\n' * 30) + '━━━━━━━━━━━━━━━━━━━━\n8. 끝나지 않은 일을 의식합니다. 이런 요소 때문에 만든 작업보드를 평가해 주세요.'
        info=scan._new_info()
        scan._feed(info,json.dumps({'type':'user','message':{'content':text}}))
        self.assertEqual(info['tasks'][-1].get('request'),'이런 요소 때문에 만든 작업보드를 평가해 주세요.')

    def test_reads_after_changes_remain_reads(self):
        task = scan._new_task('읽기 수정 읽기', 1)
        for ts, change, text in [(2, False, '조회'), (3, True, '수정'), (4, False, '조회'), (5, False, '테스트')]:
            scan._record(task, ts, change, text, True)
        self.assertEqual(task['n_steps'], 1)
        self.assertEqual(task['n_checks'], 3)
        self.assertEqual([s['text'] for s in task['steps']], ['수정'])
        self.assertEqual([s['text'] for s in task['checks']], ['조회', '조회', '테스트'])

    def test_successful_question_answer_does_not_reappear_from_finished_message(self):
        t = scan._new_task('수정', 1)
        scan._note(t, 2, '공유 링크: https://example.com/?usp=sharing')
        scan._close(t, 3)
        self.assertEqual(scan._finish([t], False, 4)[0]['ask'], '')

    def test_jira_completion_timestamp_is_preserved(self):
        payload = {'fields': {'summary': '[BE] 백업 정리', 'status': {'name': '완료', 'statusCategory': {'key': 'done'}},
                              'resolutiondate': '2026-11-16T09:00:00.000+0900'}}
        with patch.object(enrich, '_run', return_value=json.dumps(payload)):
            self.assertEqual(enrich._jira('MP-1').get('resolved_at'), '2026-11-16T09:00:00.000+0900')

    def test_known_completion_time_survives_a_jira_refresh_failure(self):
        old={'title':'백업 정리','status':'완료','cat':'done','resolved_at':'2026-11-16T09:00:00.000+0900'}
        with patch.dict(enrich._data,{'MP-1':old}), patch.object(enrich,'_jira',return_value={}), \
                patch.object(enrich,'_mrs',return_value=[]), patch.object(enrich,'_commits',return_value=[]), \
                patch.object(enrich,'_save'):
            enrich._refresh('MP-1')
            self.assertEqual(enrich._data['MP-1'].get('resolved_at'),old['resolved_at'])


class WorkAttributionTests(unittest.TestCase):
    def setUp(self):
        title = patch.object(scan.title_summary, 'get', return_value={'state': 'pending', 'title': ''})
        title.start()
        self.addCleanup(title.stop)

    def works(self, tasks, aliases=None):
        with patch.dict(scan._jira,{'MP-4549':'개발 배포 DB 백업 누적'}), \
                patch.object(scan,'ALIASES',aliases or {}), \
                patch.object(enrich,'story',return_value={'title':'개발 배포 DB 백업 누적','cat':'done'}):
            return scan._works(tasks,'',{})

    def make_task(self,prompt,area,ts):
        t=scan._new_task(prompt,ts)
        t['branch']='HEAD'
        t['areas']={area:3} if area else {}
        return t

    def test_document_after_new_console_task_never_returns_to_old_backup_issue(self):
        tasks=[self.make_task('백업을 최근 3개만 보관','MP-4549',1),
               self.make_task('로키9.5 콘솔 한글 표시 문제 조사','ti-withvtm',2),
               self.make_task('ini들도 봐야 되는 거 아니야?',None,3),
               self.make_task('이전 이후 html로 만들어 봐','문서/패키징_릴리스/vtmctl_패키징',4)]
        works=self.works(tasks)
        old=next(w for w in works if w['story']=='MP-4549')
        current=next(w for w in works if 3 in w['tasks'])
        self.assertEqual(old['tasks'],[0])
        self.assertEqual(current['tasks'],[1,2,3])
        self.assertEqual(current['story'],'')

    def test_document_needs_evidence_to_attach_to_previous_jira(self):
        tasks=[self.make_task('백업 정리','MP-4549',1),self.make_task('새 화면 목업','문서/화면/새_화면',2)]
        works=self.works(tasks)
        self.assertEqual(next(w for w in works if w['story']=='MP-4549')['tasks'],[0])

    def test_explicit_issue_in_document_path_preserves_its_association(self):
        tasks=[self.make_task('백업 검증 문서','문서/패키징_릴리스/백업_MP-4549',1)]
        self.assertEqual(self.works(tasks)[0]['story'],'MP-4549')

    def test_manual_document_alias_is_preserved(self):
        area='문서/화면/백업'
        tasks=[self.make_task('백업 정리','MP-4549',1),self.make_task('백업 결과 문서',area,2)]
        self.assertEqual(self.works(tasks,{area:'MP-4549'})[0]['tasks'],[0,1])

    def test_long_user_request_is_never_used_verbatim_as_a_work_title(self):
        request='그럼 이제 그거 해야돼 우선 로키9.5 콘솔 환경에서 한글 안 나오는 문제를 사람들이 어떻게 처리하고 있는지 공식문서나 현업 조사 후 우리 vtm과 ti에서 어느 부분을 수정해야되는지 조사'
        work=self.works([self.make_task(request,'ti-withvtm',1)])[0]
        self.assertLessEqual(len(work['title']),40)
        self.assertNotIn('그럼 이제',work['title'])
        self.assertNotIn('…',work['title'])

    def test_mid_turn_user_steering_changes_title_context_but_tool_notes_do_not(self):
        task=self.make_task('콘솔 문제 조사','repo',1)
        with patch.object(scan.title_summary,'get',return_value={'state':'ready','title':'콘솔 문제 조사'}) as summary:
            self.works([task])
            before=summary.call_args.args[0]
            task['notes'].append({'ts':2,'text':'조사를 진행합니다.'})
            self.works([task])
            self.assertEqual(summary.call_args.args[0],before)
            task['extra'].append({'ts':3,'text':'이제 설치 오류 수정으로 바꿔 주세요.'})
            self.works([task])
            after=summary.call_args.args[0]
            self.assertNotEqual(after,before)
            self.assertIn('이제 설치 오류 수정으로 바꿔 주세요.',after['turns'][-1]['followups'])

    def test_every_unlabelled_area_uses_summary_including_work_board(self):
        for area in ('work-board','repo','another-project'):
            with self.subTest(area=area):
                work=self.works([self.make_task('작업 상태 판정을 고쳐 주세요.',area,1)])[0]
                self.assertEqual(work.get('title_source'),'summary')

    def test_title_context_keeps_requests_separate_from_quoted_followups(self):
        task=self.make_task('모니터링 화면을 개선해 주세요.','dashboard',1)
        task['extra']=[{'ts':2,'text':'과거 업무 설명 '*100,'request':'이전 작업이 왜 여기에 표시되나요?'}]
        task2=self.make_task('진척도도 표시해 주세요.','dashboard',3)
        with patch.object(scan.title_summary,'get',return_value={'state':'ready','title':'모니터링 화면 개선'}) as summary:
            self.works([task,task2])
            context=summary.call_args.args[0]
            self.assertEqual(context['objective'],'모니터링 화면을 개선해 주세요.')
            self.assertEqual(context['turns'][0]['followups'],['이전 작업이 왜 여기에 표시되나요?'])
            self.assertEqual(context['turns'][-1]['request'],'진척도도 표시해 주세요.')


class CurrentContextTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.enterContext(document_connection(self.root))

    def test_codex_followup_keeps_tail_and_successful_edit_links_current_plan(self):
        c=sqlite3.connect(':memory:')
        self.addCleanup(c.close)
        c.execute('CREATE TABLE thread_items (thread_id,turn_id,item_type,item_json,created_at_ms,rollout_ordinal)')
        feedback='이전 업무의 긴 설명입니다. '*100+'지금 제목이 왜 다르게 나오나요?'
        plan=str(self.root/'화면/개선/계획서/구현.md')
        messages=[('userMessage',{'content':[{'text':'보드를 수정해 주세요.'}]}),
                  ('userMessage',{'content':[{'text':feedback}]}),
                  ('commandExecution',{'command':'cat '+plan,'cwd':str(self.root)})]
        for i,(kind,data) in enumerate(messages):
            c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('thread','turn',kind,json.dumps(data),1000+i,i))
        task=scan._codex_turn(c,'thread','turn','inProgress',1,0)
        self.assertIn('왜 다르게 나오나요?',task['extra'][-1]['request'])
        self.assertNotIn(plan,task['plan_refs'])
        c.execute('INSERT INTO thread_items VALUES (?,?,?,?,?,?)',('thread','turn','fileChange',json.dumps({'status':'completed','changes':[{'path':plan}]}),1004,4))
        task=scan._codex_turn(c,'thread','turn','inProgress',1,0)
        self.assertIn(os.path.realpath(plan), task['plan_refs'])

    def test_queued_user_feedback_keeps_tail_of_long_pasted_example(self):
        info=scan._new_info()
        scan._open_task(info,'화면 수정',1)
        feedback='오래된 업무 설명입니다. '*100+'지금은 다른 작업인데 왜 이 제목으로 나오나요?'
        scan._feed(info,json.dumps({'type':'attachment','attachment':{'type':'queued_command',
            'origin':{'kind':'human'},'prompt':feedback}}))
        self.assertIn('왜 이 제목으로 나오나요?',info['tasks'][-1]['extra'][-1].get('request',''))

    def test_progress_report_excludes_a_question_but_preserves_work_in_same_message(self):
        t=scan._new_task('화면 수정',1)
        scan._note(t,2,'원인을 찾고 있습니다.')
        scan._note(t,3,'진척도는 어떤 모습이 더 좋으신가요?')
        self.assertEqual(scan._finish([t],True,4)[0]['now'],'원인을 찾고 있습니다.')
        scan._note(t,5,'현재 자료를 확인하고 있습니다. 어느 방식이 좋으신가요?')
        self.assertEqual(scan._finish([t],True,6)[0]['now'],'현재 자료를 확인하고 있습니다.')

    def test_question_choices_do_not_appear_as_current_work(self):
        text='진척 표시는 어떤 방식이 좋으신가요?\n1. 단계별 완료 표시\n2. 현재 활동 표시\n\n실제 계획 파일을 읽고 있습니다.'
        self.assertEqual(scan._activity_text(text),'실제 계획 파일을 읽고 있습니다.')

    def test_feedback_tail_survives_more_than_ten_thousand_characters(self):
        raw='참고로 붙인 예전 업무 내용입니다. '*1000+'왜 저 제목이 표시되나요?'
        self.assertIn('왜 저 제목이 표시되나요?',scan._request_excerpt(raw))


if __name__ == '__main__':
    unittest.main()
