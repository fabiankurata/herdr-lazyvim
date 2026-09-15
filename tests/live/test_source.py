import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from source import checked_source, exact_revision, new_artifact, source_archive
from owned_session import isolated_environment

HARNESS = Path(__file__).resolve().parents[2]


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hf-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.env = isolated_environment(self.root)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'fixture.txt').write_text('synthetic\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.sha = self.git('rev-parse', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo, env=self.env, text=True).strip()

    def test_exact_sha_and_dirty_checkout(self):
        self.assertEqual(checked_source(self.repo, self.sha)['revision'], self.sha)
        for revision in ('HEAD', self.sha[:12], '0' * 40):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                exact_revision(self.repo, revision)
        (self.repo / 'fixture.txt').write_text('dirty\n')
        with self.assertRaisesRegex(ValueError, 'dirty'):
            checked_source(self.repo, self.sha)
        self.git('checkout', '--', 'fixture.txt')
        (self.repo / 'untracked').touch()
        with self.assertRaisesRegex(ValueError, 'dirty'):
            checked_source(self.repo, self.sha)

    def test_archive_exact_despite_dirty_working_file(self):
        (self.repo / 'fixture.txt').write_text('not committed\n')
        with source_archive(self.repo, self.sha) as (source, provenance):
            self.assertEqual((source / 'fixture.txt').read_text(), 'synthetic\n')
            self.assertEqual(provenance['tree'], self.git('rev-parse', self.sha + '^{tree}'))
            self.assertEqual(len(provenance['archive_sha256']), 64)
        self.assertFalse(source.exists())

    def run_lane(self, scenario, path, *args):
        return subprocess.run([sys.executable, str(HARNESS / 'tests/live/run.py'),
                               '--source-repo', str(self.repo), '--revision', self.sha,
                               '--scenario', scenario, '--artifact-dir', str(path), *args],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def test_actual_entrypoint_rejects_target_independent_of_name(self):
        sentinel = self.root / 'caller.sock'
        sentinel.write_bytes(b'caller-owned')
        self.env['HERDR_SOCKET_PATH'] = str(sentinel)
        for index, scenario in enumerate(('boot-isolation', 'default-session-guard', 'anything')):
            artifact = self.root / str(index)
            result = self.run_lane(scenario, artifact, '--real', '--socket', str(sentinel))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('caller socket', result.stderr)
            self.assertEqual(json.loads((artifact / 'result.json').read_text())['status'], 'FAIL')
        self.assertEqual(sentinel.read_bytes(), b'caller-owned')

    def test_scenario_names_never_fake_pass(self):
        for index, scenario in enumerate(('default-session-guard', 'nonexistent', 'warm-toggle')):
            artifact = self.root / str(index)
            result = self.run_lane(scenario, artifact)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads((artifact / 'result.json').read_text())['status'], 'UNVERIFIED')

    def test_plan_artifact_path_and_existing_evidence(self):
        artifact = self.root / 'artifacts/PR00' / self.sha / 'lane-2'
        result = self.run_lane('isolation', artifact)
        self.assertEqual(result.returncode, 0, result.stderr)
        pty = json.loads((artifact / 'capture/pty/pty.json').read_text())
        self.assertEqual(pty['sent_hex'], pty['received_hex'])
        previous = (artifact / 'result.json').read_bytes()
        result = self.run_lane('isolation', artifact)
        self.assertEqual(result.returncode, 1)
        self.assertEqual((artifact / 'result.json').read_bytes(), previous)
        with self.assertRaises(FileExistsError):
            new_artifact(artifact)


if __name__ == '__main__':
    unittest.main()
