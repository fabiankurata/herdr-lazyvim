import fcntl
import importlib.util
import json
from pathlib import Path
import pty
import struct
import tempfile
import termios
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name('live_pane.py')
SPEC = importlib.util.spec_from_file_location('live_pane_viewport', SOURCE)
LIVE_PANE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LIVE_PANE)


class NativeViewportHelpersTests(unittest.TestCase):
    def test_render_marker_is_compact_unique_synthetic_text(self):
        with mock.patch.object(LIVE_PANE.secrets, 'token_hex', return_value='abcdef123456'):
            self.assertEqual(LIVE_PANE.render_marker(), 'PR00CAP-ABCDEF123456')

    def test_pty_grid_observes_the_configured_rows_and_columns(self):
        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 48, 160, 0, 0))
            self.assertEqual(LIVE_PANE.pty_grid(slave), [48, 160])
        finally:
            import os
            os.close(master)
            os.close(slave)

    def test_process_identity_requires_complete_ps_row(self):
        self.assertEqual(
            LIVE_PANE.parse_process_identity('123 Mon Sep 14 10:11:12 2026 /usr/local/bin/herdr\n'),
            {'pid': 123, 'start': 'Mon Sep 14 10:11:12 2026', 'executable': '/usr/local/bin/herdr'},
        )
        with self.assertRaisesRegex(RuntimeError, 'identity is unavailable'):
            LIVE_PANE.parse_process_identity('123 Mon Sep 14\n')

    def test_visibility_requires_the_requested_focused_tab_and_editor_membership(self):
        snapshot = {'focused_tab_id': 'display', 'panes': [{'pane_id': 'editor', 'tab_id': 'display'}]}
        self.assertEqual(LIVE_PANE.editor_visibility(snapshot, 'editor', 'display')['state'], 'visible')
        self.assertEqual(LIVE_PANE.editor_visibility(snapshot | {'focused_tab_id': 'other'}, 'editor', 'display')['state'], 'invalid')
        self.assertEqual(LIVE_PANE.editor_visibility({'focused_tab_id': 'display', 'panes': []}, 'editor', 'display')['state'], 'invalid')
        hidden = LIVE_PANE.editor_visibility({'focused_tab_id': 'display', 'panes': [{'pane_id': 'editor', 'tab_id': 'parked'}]}, 'editor', 'display')
        self.assertEqual(hidden['state'], 'hidden')

    def test_preflight_failure_persists_diagnostics_without_invoking_capture(self):
        with tempfile.TemporaryDirectory() as root:
            for name, failure in [('timeout', 'did not settle'), ('absent', 'editor identity is absent or ambiguous')]:
                calls = []
                context = {'viewport_before': {'visibility': {'state': 'visible'}, 'failure': failure}}
                with self.assertRaisesRegex(AssertionError, 'preflight failed'):
                    LIVE_PANE.invoke_native_checkpoint(lambda *args: calls.append(args), root, name, object(), context)
                self.assertEqual(calls, [])
                self.assertEqual(json.loads((Path(root) / (name + '.preflight.json')).read_text())['failure'], failure)

    def test_valid_visible_and_hidden_preflights_invoke_capture(self):
        with tempfile.TemporaryDirectory() as root:
            calls = []
            for name, state in [('visible', 'visible'), ('hidden', 'hidden')]:
                context = {'viewport_before': {'visibility': {'state': state}}}
                LIVE_PANE.invoke_native_checkpoint(lambda *args: calls.append(args), root, name, object(), context)
            self.assertEqual(len(calls), 2)

    def test_visible_match_settles_without_recovery(self):
        matched = {'content_grid': [18, 54], 'editor_pty_grid': [18, 54], 'nvim_grid': [18, 54]}
        observations = iter([matched, matched])
        recovery = []
        result = LIVE_PANE.settle_visible_geometry(lambda: next(observations), lambda: recovery.append('nudge'), sleep=lambda _: None)
        self.assertEqual(recovery, [])
        self.assertEqual(result['settled'], matched)

    def test_visible_mismatch_recovers_once_then_settles(self):
        observations = iter([
            {'content_grid': [18, 55], 'editor_pty_grid': [18, 54], 'nvim_grid': [18, 54]},
            {'content_grid': [18, 54], 'editor_pty_grid': [18, 54], 'nvim_grid': [18, 54]},
            {'content_grid': [18, 54], 'editor_pty_grid': [18, 54], 'nvim_grid': [18, 54]},
        ])
        recovery = []
        result = LIVE_PANE.settle_visible_geometry(lambda: next(observations), lambda: recovery.append('nudge') or {'calls': 2})
        self.assertEqual(recovery, ['nudge'])
        self.assertEqual(result['settled']['nvim_grid'], [18, 54])
        self.assertEqual(result['recovery'], {'calls': 2})

    def test_persistent_mismatch_fails_after_one_recovery(self):
        now = [0]
        calls = []
        mismatch = {'content_grid': [18, 55], 'editor_pty_grid': [18, 54], 'nvim_grid': [18, 54]}
        result = LIVE_PANE.settle_visible_geometry(
            lambda: mismatch, lambda: calls.append('nudge') or {'calls': 2}, deadline_seconds=1,
            clock=lambda: now[0], sleep=lambda _: now.__setitem__(0, now[0] + .6))
        self.assertIsNone(result['settled'])
        self.assertEqual(calls, ['nudge'])

    def test_observation_finishing_after_deadline_fails_without_recovery(self):
        now = [0]
        def slow_observe():
            now[0] = 2
            return {'content_grid': [18, 54], 'editor_pty_grid': [18, 54], 'nvim_grid': [18, 54]}
        calls = []
        result = LIVE_PANE.settle_visible_geometry(slow_observe, lambda: calls.append('nudge'), clock=lambda: now[0], sleep=lambda _: None)
        self.assertIsNone(result['settled'])
        self.assertEqual(calls, [])

    def test_hidden_and_invalid_preflights_do_not_observe_or_recover(self):
        calls = []
        observe = lambda: calls.append('observe')
        recover = lambda: calls.append('recover')
        hidden = {'focused_tab_id': 'display', 'panes': [{'pane_id': 'editor', 'tab_id': 'parked'}]}
        invalid = {'focused_tab_id': 'display', 'panes': []}
        self.assertEqual(LIVE_PANE.preflight_viewport(hidden, 'editor', 'display', observe, recover)['visibility']['state'], 'hidden')
        self.assertEqual(LIVE_PANE.preflight_viewport(invalid, 'editor', 'display', observe, recover)['visibility']['state'], 'invalid')
        self.assertEqual(calls, [])


    def test_native_onboarding_config_is_exclusive_and_owned(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root) / 'runtime'; config = runtime / 'config'; config.mkdir(parents=True)
            session = type('Session', (), {'root': runtime, 'env': {'XDG_CONFIG_HOME': str(config)}})()
            receipt = LIVE_PANE.write_native_onboarding_config(session, root)
            self.assertEqual((config / 'herdr/config.toml').read_text(), 'onboarding = false\n')
            self.assertTrue(receipt['sha256'])
            with self.assertRaisesRegex(AssertionError, 'already exists'):
                LIVE_PANE.write_native_onboarding_config(session, root)


if __name__ == '__main__':
    unittest.main()
