import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import board_actions as actions

class ActionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.home=self.root/'home';self.home.mkdir();self.cwd=self.root/'project';self.cwd.mkdir()
  self.state=self.root/'state';self.store=actions.ActionStore(self.state,self.home)
 def skill(self,name='review',body='Review work',agent='claude'):
  p=self.home/('.'+agent)/'skills'/name/'SKILL.md';p.parent.mkdir(parents=True,exist_ok=True);p.write_text('---\nname: '+name+'\ndescription: '+body+'\n---\nInstructions')
  return p
 def test_catalog_discovers_installed_skills_and_excludes_noninvocable(self):
  self.skill();self.skill('internal','hidden\nuser-invocable: false')
  result=self.store.catalog(self.cwd)
  self.assertEqual([s['name'] for s in result],['review'])
 def test_disabled_plugin_is_not_launchable(self):
  p=self.home/'.claude/plugins/test';(p/'commands').mkdir(parents=True);(p/'commands/do.md').write_text('---\nname: do\n---')
  (p.parent/'installed_plugins.json').write_text(json.dumps({'plugins':{'test@market':[{'scope':'user','installPath':str(p)}]}}))
  settings=self.home/'.claude/settings.json';settings.write_text(json.dumps({'enabledPlugins':{'test@market':False}}))
  self.assertEqual(self.store.catalog(self.cwd),[])
 def test_favorite_is_stable_and_removal_is_reflected(self):
  p=self.skill();item=self.store.catalog(self.cwd)[0]
  self.store.favorite(item['id'],True,self.cwd)
  self.assertTrue(actions.ActionStore(self.state,self.home).catalog(self.cwd)[0]['favorite'])
  p.unlink();self.assertEqual(self.store.catalog(self.cwd),[])
 def test_unknown_skill_cannot_supply_arbitrary_command_or_path(self):
  self.skill()
  with self.assertRaises(ValueError):self.store.launch('/tmp/evil; touch x',self.cwd,'unique-request')
 def test_launcher_quotes_paths_and_prompt_and_never_bypasses_permissions(self):
  p=self.skill('review','Review work')
  cli=self.root/'fake tool';cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$WB_TEST_ARGS"\n');cli.chmod(0o700)
  dest=self.root/'args'
  item=self.store.catalog(self.cwd)[0]
  with patch.object(actions,'executable',return_value=str(cli)),patch.object(actions,'open_terminal',return_value=None) as opener:
   launch=self.store.launch(item['id'],self.cwd,'same-click')
   again=self.store.launch(item['id'],self.cwd,'same-click')
   self.assertEqual(launch['id'],again['id']);self.assertEqual(opener.call_count,1)
  script=Path(launch['launcher']);self.assertEqual(script.stat().st_mode & 0o777,0o700)
  subprocess.run([str(script)],env={**os.environ,'WB_TEST_ARGS':str(dest)},check=True)
  args=dest.read_text()
  self.assertIn(str(p),args);self.assertIn('--session-id',args)
  self.assertNotIn('skip-permissions',args)
 def test_open_failure_is_not_reported_as_started_and_can_be_retried(self):
  self.skill();item=self.store.catalog(self.cwd)[0]
  with patch.object(actions,'executable',return_value='/bin/echo'),patch.object(actions,'open_terminal',side_effect=RuntimeError('no terminal')):
   with self.assertRaises(RuntimeError):self.store.launch(item['id'],self.cwd,'retry-click')
  with patch.object(actions,'executable',return_value='/bin/echo'),patch.object(actions,'open_terminal',return_value=None):
   self.assertEqual(self.store.launch(item['id'],self.cwd,'retry-click')['status'],'opened')
 def test_codex_launcher_preserves_project_skill_and_permissions(self):
  p=self.skill(agent='codex');cli=self.root/'fake-codex';dest=self.root/'codex-args'
  cli.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "$WB_TEST_ARGS"\n');cli.chmod(0o700)
  with patch.object(actions,'executable',return_value=str(cli)),patch.object(actions,'open_terminal'):
   item=self.store.catalog(self.cwd)[0];launch=self.store.launch(item['id'],self.cwd,'codex-click')
  subprocess.run([launch['launcher']],env={**os.environ,'WB_TEST_ARGS':str(dest)},check=True)
  args=dest.read_text().splitlines()
  self.assertEqual(Path(args[0]).resolve(),self.cwd.resolve());self.assertEqual(args[1],'-C')
  self.assertEqual(Path(args[2]).resolve(),self.cwd.resolve());self.assertIn(str(p.resolve()),args[-1])
  self.assertNotIn('dangerously',dest.read_text());self.assertNotIn('sandbox',dest.read_text())
 def test_codex_disabled_skills_and_last_matching_rule_are_respected(self):
  p=self.skill(agent='codex');config=self.home/'.codex/config.toml'
  config.write_text('[[skills.config]]\nname = "review"\nenabled = false\n')
  self.assertEqual(self.store.catalog(self.cwd),[])
  config.write_text(config.read_text()+'\n[[skills.config]]\npath = '+json.dumps(str(p))+'\nenabled = true\n')
  self.assertEqual([x['name'] for x in self.store.catalog(self.cwd)],['review'])
 def test_cannot_launch_codex_skill_disabled_after_catalog_read(self):
  self.skill(agent='codex');item=self.store.catalog(self.cwd)[0]
  (self.home/'.codex/config.toml').write_text('[[skills.config]]\nname="review"\nenabled=false\n')
  with self.assertRaises(ValueError):self.store.launch(item['id'],self.cwd,'stale-choice')

 def test_new_terminal_preserves_prompt_as_data_for_both_agents(self):
  for agent in ['claude','codex']:
   self.skill(agent=agent);cli=self.root/('fake-'+agent);dest=self.root/(agent+'-prompt.json');marker=self.root/'should-not-exist'
   cli.write_text('#!/usr/bin/python3\nimport json,os,sys\nopen(os.environ["WB_TEST_ARGS"],"w").write(json.dumps(sys.argv[1:]))\n');cli.chmod(0o700)
   prompt='한글 요청\n"인용" `touch '+str(marker)+'` $(touch '+str(marker)+')'
   item=next(s for s in self.store.catalog(self.cwd) if s['agent']==agent)
   with patch.object(actions,'executable',return_value=str(cli)),patch.object(actions,'open_terminal') as opener:
    result=self.store.launch(item['id'],self.cwd,'prompt-'+agent,prompt)
    self.store.launch(item['id'],self.cwd,'prompt-'+agent,prompt)
    with self.assertRaises(ValueError):self.store.launch(item['id'],self.cwd,'prompt-'+agent,'바뀐 요청')
    self.assertEqual(opener.call_count,1)
   subprocess.run([result['launcher']],env={**os.environ,'WB_TEST_ARGS':str(dest)},check=True)
   args=json.loads(dest.read_text());self.assertTrue(args[-1].endswith(prompt));self.assertFalse(marker.exists())
