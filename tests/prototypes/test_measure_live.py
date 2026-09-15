import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).with_name('measure_live.py')
SPEC = importlib.util.spec_from_file_location('measure_live', MODULE)
measure_live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(measure_live)


class FailureReceiptTests(unittest.TestCase):
    def receipt_root(self, root):
        evidence = root / 'owned-session-fixture'
        evidence.mkdir()
        (evidence / 'default-before.json').write_text(json.dumps({'sockets': {'default': None}, 'processes': {}}))

    def test_retains_literal_baseline_when_observed_state_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.receipt_root(root)
            capture = root / 'agent-input.bin'
            fixture = root / 'fixture.txt'
            capture.write_bytes(b'wrong input\n')
            fixture.write_bytes(b'changed fixture\n')
            calls = []
            def api(*args):
                calls.append(args)
                return {'process_info': {'shell_pid': 99 if args[-1] == 'editor' else 98}}
            baseline = {
                'editor_before': {'pid': 41, 'dirty': True, 'lines': ['unsaved fixture'], 'lsp': 1},
                'editor_pid': 41, 'agent_before': {'shell_pid': 42}, 'agent_pid': 42, 'lsp_pid': 43,
                'saved_fixture': {'hex': b'saved fixture\n'.hex()},
                'expected_fake_input_prefix': {'status': 'COMPLETED', 'hex': b'fixture-before\n'.hex()},
            }
            result = measure_live.retain_failure(
                {'status': 'FAIL', 'failed_geometry': [{'elapsed_ms': 1000}], 'retention_baseline': baseline},
                AssertionError('geometry did not converge within one overall second'), root,
                api=api, rpc=lambda: {'pid': 99, 'dirty': False, 'lines': ['changed fixture'], 'lsp': 0},
                editor='editor', agent='agent', capture=capture, fixture=fixture,
                retention_baseline=baseline)
            self.assertEqual(result['error'], "AssertionError('geometry did not converge within one overall second')")
            self.assertEqual(result['failed_geometry'], [{'elapsed_ms': 1000}])
            self.assertEqual(result['failure_state']['status'], 'COLLECTED')
            self.assertEqual(result['failure_state']['retention_baseline'], baseline)
            self.assertEqual(result['failure_state']['fields']['editor_dirty_text_lsp']['value']['dirty'], False)
            self.assertEqual(result['failure_state']['fields']['fake_input']['value']['hex'], b'wrong input\n'.hex())
            self.assertEqual(result['failure_state']['fields']['saved_fixture']['value']['hex'], b'changed fixture\n'.hex())
            protected = result['failure_state']['fields']['protected_default_before']['value']
            self.assertEqual(protected['before'], {'sockets': {'default': None}, 'processes': {}})
            self.assertTrue(protected['final_comparison_authority']['default_after_path'].endswith('default-after.json'))
            self.assertEqual(calls, [('pane', 'process-info', '--pane', 'editor'),
                                     ('pane', 'process-info', '--pane', 'agent')])

    def test_early_failure_marks_unexercised_baseline_and_diagnostics_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = measure_live.retain_failure(
                {'status': 'FAIL'}, RuntimeError('original failure'), root,
                api=lambda *unused: (_ for _ in ()).throw(OSError('process info unavailable')),
                rpc=lambda: (_ for _ in ()).throw(OSError('editor unavailable')),
                editor='editor', agent='agent', capture=root / 'missing-input.bin')
            self.assertEqual(result['error'], "RuntimeError('original failure')")
            fields = result['failure_state']['fields']
            self.assertEqual(result['failure_state']['retention_baseline']['status'], 'UNEXERCISED')
            self.assertEqual(fields['editor_identity']['status'], 'UNAVAILABLE')
            self.assertEqual(fields['agent_identity']['status'], 'UNAVAILABLE')
            self.assertEqual(fields['editor_dirty_text_lsp']['status'], 'UNAVAILABLE')
            self.assertEqual(fields['fake_input']['status'], 'UNAVAILABLE')
            self.assertEqual(fields['saved_fixture']['status'], 'UNAVAILABLE')
            self.assertEqual(fields['protected_default_before']['status'], 'UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
