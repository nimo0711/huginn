"""Release gates exercise outputs, not the spelling of workflow files."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts import release_tools


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'name':'Forkrail', 'display_name':'Huginn', 'version':'1.2.0-rc.1',
                       'package_version':'1.2.0', 'bundle_id':'app.forkrail.desktop',
                       'repository':'nimo0711/forkrail',
                       'platforms':['macos-arm64','macos-x86_64','windows-x64']}

    def test_tag_must_match_the_version_being_built(self):
        release_tools.validate_config(self.config, tag='v1.2.0-rc.1')
        for tag in ['v1.1.3','v1.2.0','../../other']:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release_tools.validate_config(self.config, tag=tag)

    def test_filename_and_installer_versions_reject_path_or_argument_injection(self):
        for field, value in [('version','../leak'), ('package_version','1.2.0; touch x'),
                             ('name','../../Other'),('display_name','../Huginn'),('display_name','Huginn --flag'),('bundle_id','app/forkrail')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                release_tools.validate_config({**self.config,field:value})

    def test_public_artifact_names_use_display_name_without_changing_identity(self):
        for target in self.config['platforms']:
            self.assertEqual(release_tools.artifact_stem(self.config,target), 'Huginn-1.2.0-rc.1-'+target)
        self.assertEqual(self.config['name'],'Forkrail')
        with self.assertRaises(ValueError):release_tools.artifact_stem(self.config,'../other')

    def test_repository_must_be_an_exact_github_owner_and_name(self):
        for repository in ['', '../forkrail', 'owner/repo/releases', 'https://evil.example/repo', 'owner/repo?token=x', None]:
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                release_tools.validate_config({**self.config,'repository':repository})

    def test_stage_contains_only_distribution_code_and_brand_resources(self):
        for name in release_tools.SOURCE_FILES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('source',encoding='utf-8')
        for name in ['board.db','.actions/connections.json','work_titles.json','server.log',
                     '.env.local','private.py','tests/personal.json']:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('PRIVATE',encoding='utf-8')
        output=self.root/'stage'
        release_tools.stage_sources(self.root,output)
        self.assertTrue((output/'desktop_service.py').is_file())
        self.assertTrue((output/'app/assets/forkrail-icon.png').is_file())
        self.assertFalse(any(b'PRIVATE' in p.read_bytes() for p in output.rglob('*') if p.is_file()))

    def test_missing_distribution_source_aborts_instead_of_silently_omitting_feature(self):
        with self.assertRaises(FileNotFoundError):
            release_tools.stage_sources(self.root,self.root/'out')

    def test_symlink_cannot_package_files_outside_the_repository(self):
        for name in release_tools.SOURCE_FILES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('ok')
        outside=self.root/'credential';outside.write_text('secret')
        target=self.root/'index.html';target.unlink()
        try:target.symlink_to(outside)
        except OSError:self.skipTest('Creating symlinks requires developer mode on Windows')
        with self.assertRaises(ValueError):release_tools.stage_sources(self.root,self.root/'out')

    def test_checksums_cover_exact_artifacts_and_detect_tampering(self):
        a=self.root/'Forkrail-1.2.0-rc.1-windows-x64.zip';a.write_bytes(b'actual installer bytes')
        release_tools.write_checksums(self.root,[a])
        release_tools.verify_checksums(self.root)
        text=(self.root/'SHA256SUMS').read_text()
        self.assertIn(hashlib.sha256(a.read_bytes()).hexdigest()+'  '+a.name,text)
        a.write_bytes(b'changed')
        with self.assertRaises(ValueError):release_tools.verify_checksums(self.root)

    def test_checksum_manifest_cannot_read_paths_outside_output(self):
        (self.root/'SHA256SUMS').write_text('0'*64+'  ../private\n')
        with self.assertRaises(ValueError):release_tools.verify_checksums(self.root)

    def test_signed_release_requires_an_actual_identity(self):
        with self.assertRaises(ValueError):release_tools.validate_signing('macos-arm64',True,{})
        with self.assertRaises(ValueError):release_tools.validate_signing('windows-x64',True,{})
        release_tools.validate_signing('macos-arm64',False,{})
        release_tools.validate_signing('windows-x64',False,{})

    def test_dmg_signing_uses_application_identity_without_installer_certificate(self):
        release_tools.validate_signing('macos-arm64',True,{
            'FORKRAIL_APPLE_APP_IDENTITY':'Developer ID Application: test',
            'FORKRAIL_NOTARY_PROFILE':'test-profile',
        })

    def test_archive_includes_single_app_root_and_preserves_executable(self):
        app=self.root/'Forkrail.app';exe=app/'Contents/MacOS/Forkrail'
        exe.parent.mkdir(parents=True);exe.write_bytes(b'executable');exe.chmod(0o755)
        dest=self.root/'app.zip';release_tools.zip_tree(app,dest)
        with zipfile.ZipFile(dest) as archive:
            info=archive.getinfo('Forkrail.app/Contents/MacOS/Forkrail')
            self.assertEqual(archive.read(info),b'executable')
            if os.name!='nt':self.assertTrue(info.external_attr >> 16 & 0o111)

    def test_native_dependency_newer_than_supported_macos_aborts_release(self):
        old='Load command 10\n      cmd LC_BUILD_VERSION\n platform 1\n    minos 11.0\n      sdk 26.2\n'
        new='Load command 10\n      cmd LC_BUILD_VERSION\n platform 1\n    minos 26.1\n      sdk 26.2\n'
        release_tools.check_macos_minimum(old,'compatible.dylib',(13,0))
        with self.assertRaises(ValueError):release_tools.check_macos_minimum(new,'incompatible.dylib',(13,0))
        with self.assertRaises(ValueError):release_tools.check_macos_minimum('unrecognized','unknown',(13,0))


if __name__=='__main__':unittest.main()
