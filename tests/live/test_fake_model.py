import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from runtime_lease import require_runtime_lease
require_runtime_lease()

from fake_model import initial_state


class ModelTests(unittest.TestCase):
    def test_ownership_topology_bytes_and_failed_mutations(self):
        with tempfile.TemporaryDirectory(prefix='hf-', dir='/tmp') as directory:
            root = Path(directory)
            model = root / 'model.json'
            state = initial_state()
            model.write_text(json.dumps(state))
            env = os.environ | {'HERDR_TEST_MODEL': str(model), 'HERDR_TEST_CALLS': str(root / 'calls')}
            def run(*args):
                return subprocess.run([str(Path(__file__).with_name('fake-herdr')), *args],
                                      env=env, capture_output=True, timeout=3)
            for args in (('pane', 'zoom', 'w:p1', '--on'),
                         ('pane', 'move', 'w:p1', '--tab', 'w:t2'),
                         ('pane', 'zoom', 'w:p1', '--on'), ('pane', 'focus', 'w:p1'),
                         ('pane', 'send', 'w:p1', '--hex', '000aff')):
                self.assertEqual(run(*args).returncode, 0)
            state = json.loads(model.read_text())
            self.assertEqual(state['panes']['w:p1']['process_id'], 'fake-agent-1')
            self.assertEqual(state['panes']['w:p1']['tab_id'], 'w:t2')
            self.assertNotIn('w:t1', state['zoomed'])
            self.assertEqual(state['zoomed']['w:t2'], 'w:p1')
            self.assertEqual(state['panes']['w:p1']['input_hex'], '000aff')
            self.assertEqual(run('pane', 'close', 'w:p1').returncode, 0)
            state = json.loads(model.read_text())
            self.assertEqual(state['panes'], {})
            self.assertIsNone(state['focused_pane'])
            self.assertFalse(any(pane_id not in state['panes'] for pane_id in state['zoomed'].values()))
            state = initial_state()
            model.write_text(json.dumps(state))
            self.assertEqual(run('pane', 'move', 'w:p1', '--tab', 'w:t2').returncode, 0)
            state = json.loads(model.read_text())
            for phase, target in (('before', 'w:t2'), ('after', 'w:t3')):
                state['fail_next'] = phase
                model.write_text(json.dumps(state))
                self.assertEqual(run('pane', 'move', 'w:p1', '--tab', 'w:t3').returncode, 1)
                state = json.loads(model.read_text())
                self.assertEqual(state['panes']['w:p1']['tab_id'], target)
            self.assertEqual(run('unsupported', 'operation').returncode, 1)

    def test_fake_agent_exact_bytes(self):
        with tempfile.TemporaryDirectory(prefix='hf-', dir='/tmp') as directory:
            output = Path(directory) / 'bytes.hex'
            payload = b'\x00\xff\nfixture\x1b[31m'
            subprocess.run([str(Path(__file__).with_name('fake-agent'))], input=payload,
                           env=os.environ | {'HERDR_TEST_AGENT_BYTES': str(output)}, check=True, timeout=3)
            self.assertEqual(bytes.fromhex(output.read_text()), payload)
