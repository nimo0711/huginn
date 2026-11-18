"""A draft cannot attach an untested or different platform's binaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from release_tools import write_checksums, sha256
from verify_release import verify
from publish_draft import prepare_assets


class ReleaseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        names=['Forkrail-1.2.0-rc.1-macos-arm64.pkg','Forkrail-1.2.0-rc.1-macos-arm64.zip']
        for name in names:(self.root/name).write_bytes(b'fixture package')
        self.manifest={'name':'Forkrail','version':'1.2.0-rc.1','target':'macos-arm64','signed':False,'artifacts':names}
        (self.root/'release-manifest.json').write_text(json.dumps(self.manifest))
        write_checksums(self.root,[*(self.root/n for n in names),self.root/'release-manifest.json'])

    def test_a_different_platform_cannot_satisfy_a_matrix_slot(self):
        with self.assertRaises(ValueError):verify(self.root,expected_target='windows-x64')

    def test_release_requires_real_service_and_ui_evidence(self):
        with self.assertRaises(ValueError):verify(self.root,require_smoke=True)
        evidence={'target':'macos-arm64','version':'1.2.0-rc.1','user_data_preserved':True,
                  'installer':'installed on disposable runner','app':{
                      'service':{'ok':True,'mode':'service','version':'1.2.0-rc.1'},
                      'ui':{'ok':False,'mode':'ui','version':'1.2.0-rc.1'}}}
        (self.root/'smoke-result.json').write_text(json.dumps(evidence))
        with self.assertRaises(ValueError):verify(self.root,require_smoke=True)
        evidence['app']['ui']['ok']=True
        evidence['portable']=evidence['app']
        (self.root/'smoke-result.json').write_text(json.dumps(evidence))
        verify(self.root,require_smoke=True,expected_target='macos-arm64')

    def test_publication_checksums_use_downloadable_names_and_exact_source(self):
        evidence={'target':'macos-arm64','version':self.manifest['version'],'user_data_preserved':True,
                  'installer':'installed on disposable runner','app':{
                      mode:{'ok':True,'mode':mode,'version':self.manifest['version']} for mode in ['service','ui']}}
        evidence['portable']=evidence['app']
        (self.root/'smoke-result.json').write_text(json.dumps(evidence))
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary)
            with self.assertRaises(ValueError):prepare_assets(self.root,'macos-arm64',destination,False,'a'*40)
            self.manifest.update(source_commit='a'*40,source_dirty=False)
            (self.root/'release-manifest.json').write_text(json.dumps(self.manifest))
            write_checksums(self.root,[*(self.root/n for n in self.manifest['artifacts']),self.root/'release-manifest.json'])
            assets=prepare_assets(self.root,'macos-arm64',destination,False,'a'*40)
            downloaded={path.name:path for path in assets}
            for row in (destination/'macos-arm64-SHA256SUMS').read_text().splitlines():
                digest,name=row.split('  ')
                self.assertIn(name,downloaded)
                self.assertEqual(digest,sha256(downloaded[name]))
            self.assertIn('macos-arm64-smoke-result.json',downloaded)


if __name__=='__main__':unittest.main()
