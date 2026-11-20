import subprocess
import unittest
from urllib.parse import parse_qs, urlparse

import feedback


class Result:
    def __init__(self, returncode, stdout=''):
        self.returncode, self.stdout = returncode, stdout


class FeedbackTests(unittest.TestCase):
    def run_with(self, result=None, error=None, gh='/usr/local/bin/gh', body='화면이 멈춥니다'):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            if error:
                raise error
            return result

        outcome = feedback.submit('owner/huginn', 'bug', ' 멈춤 ', body, '1.2.0-rc.8', 'macos', runner=runner, gh=gh)
        return outcome, calls

    def test_creates_issue_with_github_cli_and_app_context_without_shell(self):
        outcome, calls = self.run_with(Result(0, 'https://github.com/owner/huginn/issues/12\n'))
        self.assertEqual((outcome['status'], outcome['url']), ('created', 'https://github.com/owner/huginn/issues/12'))
        args, kwargs = calls[0]
        self.assertEqual(args[:7], ['/usr/local/bin/gh', 'issue', 'create', '-R', 'owner/huginn', '--title', '[버그] 멈춤'])
        self.assertEqual(args[7:], ['--body-file', '-'])
        self.assertTrue(kwargs['input'].startswith('화면이 멈춥니다\n\n---\n앱 버전: 1.2.0-rc.8\nOS: macos'))
        self.assertNotIn('shell', kwargs)

    def test_falls_back_to_prefilled_browser_page_when_cli_fails_or_is_missing(self):
        for result, error, gh in [(Result(1, 'not logged in'), None, '/usr/local/bin/gh'),
                                  (Result(0, 'https://github.com/other/repo/issues/1'), None, '/usr/local/bin/gh'),
                                  (None, subprocess.TimeoutExpired('gh', 30), '/usr/local/bin/gh'),
                                  (None, None, '')]:
            outcome, calls = self.run_with(result, error, gh)
            self.assertEqual(outcome['status'], 'browser')
            url = urlparse(outcome['url'])
            query = parse_qs(url.query)
            self.assertEqual((url.netloc, url.path), ('github.com', '/owner/huginn/issues/new'))
            self.assertEqual(query['title'], ['[버그] 멈춤'])
            self.assertIn('앱 버전: 1.2.0-rc.8', query['body'][0])
            self.assertEqual(len(calls), 0 if not gh else 1)

    def test_long_feedback_keeps_browser_link_within_limit(self):
        outcome, _ = self.run_with(Result(1), body='가' * 5000)
        self.assertLessEqual(len(outcome['url']), feedback.BROWSER_URL_LIMIT)
        self.assertIn('앞부분만 채웠습니다', parse_qs(urlparse(outcome['url']).query)['body'][0])

    def test_rejects_invalid_input_and_repository_before_running_cli(self):
        def runner(*_args, **_kwargs):
            raise AssertionError('must not run')
        for repository, kind, title, body in [('owner/huginn', 'other', 't', 'b'), ('owner/huginn', 'idea', ' ', 'b'),
                                              ('owner/huginn', 'idea', 't', 'b' * 5001), ('-R x', 'idea', 't', 'b'),
                                              ('owner/huginn', 'idea', None, 'b')]:
            with self.assertRaises(ValueError):
                feedback.submit(repository, kind, title, body, '1', 'macos', runner=runner, gh='/usr/local/bin/gh')


if __name__ == '__main__':
    unittest.main()
