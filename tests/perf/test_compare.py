import contextlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare


class ComparisonTests(unittest.TestCase):
    @contextlib.contextmanager
    def sources(self, root, revision, records):
        info = {'revision': revision, 'tree': revision + '-tree'}
        records.append(info)
        yield root, info

    def test_same_sha_keeps_two_sides_and_p95_supports_100(self):
        observations = []
        for index in range(100):
            for side, value in (('baseline', index), ('candidate', index + 100)):
                observations.append({'side': side, 'revision': 'same', 'status': 'PASS',
                                     'metrics': {'duration': value}})
        self.assertEqual(compare.summary(observations, 'baseline')['metrics']['duration']['p95'], 94)
        self.assertEqual(compare.summary(observations, 'candidate')['metrics']['duration']['p95'], 194)

    def test_failed_samples_not_reported_as_speedups(self):
        stats = compare.summary([{'side': 'baseline', 'status': 'FAIL', 'metrics': {'duration': 0}}], 'baseline')
        self.assertEqual(stats['failed'], 1)
        self.assertEqual(stats['metrics'], {})

    def test_interleaved_current_callable_on_exact_archives_without_old_harness(self):
        with tempfile.TemporaryDirectory(prefix='hf-', dir='/tmp') as directory:
            root = Path(directory) / 'repository'
            root.mkdir()
            def git(*arguments):
                return subprocess.check_output(['git', *arguments], cwd=root, text=True).strip()
            subprocess.run(['git', 'init', '--quiet'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.email', 'archive@example.invalid'], cwd=root, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Archive Fixture'], cwd=root, check=True)
            pane = root / 'herdr/pane.sh'
            pane.parent.mkdir()
            pane.write_text('baseline source\n')
            subprocess.run(['git', 'add', '.'], cwd=root, check=True)
            subprocess.run(['git', 'commit', '--quiet', '-m', 'baseline'], cwd=root, check=True)
            baseline = git('rev-parse', 'HEAD')
            baseline_tree = git('rev-parse', 'HEAD^{tree}')
            pane.write_text('candidate source revision\n')
            subprocess.run(['git', 'commit', '--quiet', '-am', 'candidate'], cwd=root, check=True)
            candidate = git('rev-parse', 'HEAD')
            candidate_tree = git('rev-parse', 'HEAD^{tree}')
            out = Path(directory) / 'out'
            out.mkdir()
            seen = []
            def measure(source, runtime, artifact):
                self.assertTrue((source / 'herdr/pane.sh').exists())
                seen.append(source)
                if len(seen) == 2:
                    raise TimeoutError('observed workload failure')
                return {'status': 'PASS', 'metrics': {'actual_fixture_work': len((source / 'herdr/pane.sh').read_bytes())}}
            with contextlib.chdir(compare.HARNESS):
                observations = compare.run_pairs(root, baseline, candidate, 2, out, measure=measure)
            self.assertEqual([item['side'] for item in observations], ['baseline', 'candidate', 'candidate', 'baseline'])
            self.assertEqual(observations[1]['status'], 'FAIL')
            self.assertIn('TimeoutError', observations[1]['error'])
            self.assertEqual(len(json.loads((out / 'observations.json').read_text())), 4)
            self.assertTrue(all('tree' in item and 'archive_sha256' in item for item in observations))
            self.assertEqual({item['revision'] for item in observations if item['side'] == 'baseline'}, {baseline})
            self.assertEqual({item['revision'] for item in observations if item['side'] == 'candidate'}, {candidate})
            self.assertEqual({item['tree'] for item in observations}, {baseline_tree, candidate_tree})
            self.assertEqual(len({item['archive_sha256'] for item in observations}), 2)
            controls = list(out.glob('sample-*/owned-session-*/update-control.json'))
            self.assertEqual(len(controls), 4)
            self.assertTrue(all(json.loads(path.read_text())['applied'] == {
                'version_check': False, 'manifest_check': False} for path in controls))

    def test_ownership_failure_persists_arm_then_aborts_before_next_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / 'out'
            out.mkdir()
            source_records = []
            measured = []
            retained_root = root / 'retained-runtime'

            class FailingSession:
                def __init__(self, *unused, **kwargs):
                    self.root = retained_root
                    self.root.mkdir(exist_ok=True)
                def __enter__(self):
                    return self
                def __exit__(self, *unused):
                    raise compare.OwnershipError('protected identities changed')

            def artifact(path):
                path.mkdir(parents=True)
                return path

            with mock.patch.object(compare, 'source_archive',
                                   side_effect=lambda repo, revision: self.sources(root, revision, source_records)), \
                 mock.patch.object(compare, 'new_artifact', side_effect=artifact), \
                 mock.patch.object(compare, 'OwnedSession', FailingSession):
                with self.assertRaisesRegex(compare.OwnershipError, 'protected identities changed'):
                    compare.run_pairs(root, 'base', 'head', 2, out,
                                      measure=lambda *unused: measured.append('called') or {'status': 'PASS', 'metrics': {}})

            self.assertEqual(measured, ['called'])
            observations = json.loads((out / 'observations.json').read_text())
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0]['side'], 'baseline')
            self.assertEqual(observations[0]['status'], 'FAIL')
            self.assertIn('OwnershipError', observations[0]['error'])
            self.assertTrue(all(record.get('retain_archive') for record in source_records))

    def test_returned_workload_failures_remain_observations_and_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / 'out'
            out.mkdir()
            source_records = []
            calls = []

            class CleanSession:
                root = None
                def __init__(self, *unused, **kwargs):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *unused):
                    return False

            def artifact(path):
                path.mkdir(parents=True)
                return path

            with mock.patch.object(compare, 'source_archive',
                                   side_effect=lambda repo, revision: self.sources(root, revision, source_records)), \
                 mock.patch.object(compare, 'new_artifact', side_effect=artifact), \
                 mock.patch.object(compare, 'OwnedSession', CleanSession):
                observations = compare.run_pairs(
                    root, 'base', 'head', 2, out,
                    measure=lambda *unused: calls.append('called') or {'status': 'FAIL', 'metrics': {}})

            self.assertEqual(calls, ['called'] * 4)
            self.assertEqual([item['status'] for item in observations], ['FAIL'] * 4)


if __name__ == '__main__':
    unittest.main()
