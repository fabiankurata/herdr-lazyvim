"""Exercise real process ownership using an injected executable, never a user server."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from runtime_lease import require_runtime_lease
require_runtime_lease()
from owned_session import (OwnedSession, OwnershipError, default_identities,
                           exception_evidence, fixture_identities,
                           isolated_environment, process_table, public_diagnostic, same_process,
                           socket_identity)

FAKE = r'''
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
MODE = __MODE__
LOG = Path(__LOG__)
args = sys.argv[3:]
root = Path(os.environ['HOME']).parent
sock = Path(os.environ['HERDR_SOCKET_PATH'])
with LOG.open('a') as stream:
    stream.write(json.dumps(args) + '\n')
if args == ['--version']:
    print('fake process fixture 1')
elif args == ['status', 'server']:
    print('socket: ' + str(sock))
    print('status: ' + ('running' if MODE == 'preexisting' else 'not running'))
elif args == ['server']:
    if MODE == 'startup-timeout':
        time.sleep(30)
    sock.parent.mkdir(parents=True)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(sock))
    listener.listen()
    child = subprocess.Popen([sys.executable, '-c', 'import time\ntime.sleep(60)'], start_new_session=True)
    (root / 'child.pid').write_text(str(child.pid))
    def stop(signum, frame):
        child.terminate()
        child.wait(timeout=2)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(.05)
elif args == ['api', 'snapshot']:
    if MODE == 'api-timeout':
        time.sleep(30)
    if MODE == 'api-error':
        print(json.dumps({'error': {'code': 'not_ready'}}))
    else:
        print(json.dumps({'result': {'fixture': True}}))
elif args[:2] == ['pane', 'focus']:
    (root / 'mutation-before').write_text('mutated')
    if MODE == 'mutation-timeout':
        time.sleep(30)
    (root / 'mutation-after').write_text('completed')
    print('{}')
else:
    raise SystemExit(1)
'''


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hf-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        environment = mock.patch.dict(os.environ, isolated_environment(self.out), clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.log = self.out / "commands.jsonl"
        self.fake = self.out / "herdr"
        self.configure("normal")
        self.patch = mock.patch("owned_session.shutil.which", return_value=str(self.fake))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def configure(self, mode):
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE.replace("__MODE__", repr(mode)).replace("__LOG__", repr(str(self.log))))
        self.fake.chmod(0o700)

    def context(self, **kwargs):
        return OwnedSession(self.out, startup_timeout=2, rpc_timeout=2, teardown_timeout=2, **kwargs)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assert_stopped(self, runtime, removed=True):
        self.assertTrue(all(child.poll() is not None for child in runtime._children))
        table = process_table()
        self.assertFalse([pid for pid, record in runtime._identities.items() if same_process(record, table.get(pid))])
        self.assertEqual(runtime.root.exists(), not removed)

    def test_boot_stop_reaps_server_and_detached_pane(self):
        runtime = self.context()
        with runtime:
            child_pid = int((runtime.root / 'child.pid').read_text())
            self.assertIn(child_pid, process_table())
            self.assertEqual(runtime.run('api', 'snapshot').returncode, 0)
        self.assert_stopped(runtime)
        report = json.loads((runtime._evidence / 'teardown.json').read_text())
        self.assertTrue(report['children_reaped'])
        self.assertTrue(report['default_identities_unchanged'])

    def test_all_caller_targets_rejected_before_any_command(self):
        sentinel = self.out / 'default.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(sentinel))
        self.addCleanup(listener.close)
        identity = sentinel.stat().st_ino
        for target in (sentinel, '/tmp/herdr.sock', '../default.sock'):
            runtime = self.context(socket=target)
            with self.assertRaises(OwnershipError):
                runtime.__enter__()
            self.assert_stopped(runtime)
        for name in ('default', 'codex-pr00-caller', '../default'):
            runtime = self.context(session=name)
            with self.assertRaises(OwnershipError):
                runtime.__enter__()
            self.assert_stopped(runtime)
        self.assertEqual(self.commands(), [])
        self.assertEqual(identity, sentinel.stat().st_ino)

    def test_inherited_configuration_stripped_and_all_xdg_isolated(self):
        inherited = {'HERDR_SOCKET_PATH': str(self.out / 'default.sock'), 'HERDR_CONFIG_PATH': '/forbidden',
                     'NVIM': '/forbidden', 'NVIM_APPNAME': 'forbidden', 'NVIM_LISTEN_ADDRESS': '/forbidden',
                     'XDG_CONFIG_DIRS': '/forbidden', 'XDG_DATA_DIRS': '/forbidden', 'BASH_ENV': '/forbidden'}
        with mock.patch.dict(os.environ, inherited):
            with self.context() as runtime:
                self.assertEqual(runtime.root.parent, Path('/tmp'))
                self.assertTrue(runtime.root.name.startswith('hf-'))
                self.assertLess(len(str(runtime.socket)), 104)
                for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME',
                            'XDG_RUNTIME_DIR', 'XDG_CONFIG_DIRS', 'XDG_DATA_DIRS'):
                    self.assertTrue(Path(runtime.env[key]).is_relative_to(runtime.root))
                self.assertFalse(any(key.startswith('NVIM') for key in runtime.env))
                self.assertNotIn('HERDR_CONFIG_PATH', runtime.env)
                self.assertNotIn('BASH_ENV', runtime.env)
                self.assertEqual(runtime.env['HERDR_SOCKET_PATH'], str(runtime.socket))

    def test_update_control_is_owned_private_and_recorded_before_boot(self):
        runtime = self.context(disable_update_checks=True)
        with runtime:
            control = runtime._evidence / 'update-control.json'
            config = Path(runtime.env['XDG_CONFIG_HOME']) / 'herdr/config.toml'
            self.assertEqual(config.read_text(), '[update]\nversion_check = false\nmanifest_check = false\n')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            record = json.loads(control.read_text())
            self.assertEqual(record['path'], str(config))
            self.assertEqual(record['applied'], {'version_check': False, 'manifest_check': False})
            commands = self.commands()
            self.assertTrue(commands)
            self.assertEqual(commands[0], ['--version'])
        self.assert_stopped(runtime)

    def test_update_control_is_opt_in(self):
        with self.context(real=False) as runtime:
            self.assertFalse((Path(runtime.env['XDG_CONFIG_HOME']) / 'herdr/config.toml').exists())
            self.assertFalse((runtime._evidence / 'update-control.json').exists())

    def test_preexisting_session_refused_without_start_or_stop(self):
        self.configure('preexisting')
        runtime = self.context()
        with self.assertRaises(OwnershipError):
            runtime.__enter__()
        self.assertNotIn(['server'], self.commands())
        self.assertNotIn(['server', 'stop'], self.commands())
        self.assert_stopped(runtime, removed=False)
        shutil.rmtree(runtime.root)

    def test_existing_session_directory_refused(self):
        original = OwnedSession.run
        def run(runtime, *args, **kwargs):
            result = original(runtime, *args, **kwargs)
            if args == ('--version',):
                runtime.socket.parent.mkdir(parents=True)
            return result
        runtime = self.context()
        with mock.patch.object(OwnedSession, 'run', run):
            with self.assertRaises(OwnershipError):
                runtime.__enter__()
        self.assertNotIn(['server'], self.commands())
        self.assert_stopped(runtime, removed=False)
        shutil.rmtree(runtime.root)

    def test_unadopted_socket_preserved_while_its_owner_lives(self):
        runtime = self.context()
        original = runtime.run
        holder = None
        def run(*args, **kwargs):
            nonlocal holder
            result = original(*args, **kwargs)
            if args == ('--version',):
                runtime.socket.parent.mkdir(parents=True)
                code = ('import socket\nimport sys\nimport time\n'
                        's = socket.socket(socket.AF_UNIX)\ns.bind(sys.argv[1])\ntime.sleep(60)\n')
                holder = subprocess.Popen([sys.executable, '-c', code, str(runtime.socket)], start_new_session=True)
                deadline = time.monotonic() + 3
                while not runtime.socket.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(runtime.socket.exists())
            return result
        try:
            with mock.patch.object(runtime, 'run', run):
                with self.assertRaises(OwnershipError):
                    runtime.__enter__()
            self.assert_stopped(runtime, removed=False)
            self.assertIsNone(holder.poll())
            self.assertTrue(runtime.socket.exists())
            self.assertNotIn(['server'], self.commands())
        finally:
            if holder is not None:
                holder.terminate()
                try:
                    holder.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.wait(timeout=3)
            if runtime.root is not None and runtime.root.exists():
                shutil.rmtree(runtime.root)

    def test_failure_before_and_after_popen_and_api(self):
        for stage in ('before-spawn', 'server-started', 'ready'):
            runtime = self.context()
            original = runtime._event
            def event(name, **details):
                if name == stage:
                    raise RuntimeError('injected ' + stage)
                original(name, **details)
            with mock.patch.object(runtime, '_event', event):
                if stage == 'before-spawn':
                    with mock.patch.object(runtime, '_spawn', side_effect=OSError('injected spawn')):
                        with self.assertRaises((OSError, OwnershipError)):
                            runtime.__enter__()
                else:
                    with self.assertRaises(RuntimeError):
                        runtime.__enter__()
            self.assert_stopped(runtime)

    def test_zero_exit_api_error_is_not_readiness(self):
        self.configure('api-error')
        runtime = self.context()
        with self.assertRaisesRegex(OwnershipError, 'successful API result'):
            runtime.__enter__()
        self.assert_stopped(runtime)

    def test_startup_and_api_timeouts_reap(self):
        for mode in ('startup-timeout', 'api-timeout', 'mutation-timeout'):
            self.configure(mode)
            runtime = self.context()
            started = time.monotonic()
            with self.assertRaises((TimeoutError, subprocess.TimeoutExpired)):
                with runtime:
                    runtime.run('pane', 'focus', 'fixture')
            self.assertLess(time.monotonic() - started, 10)
            self.assert_stopped(runtime)

    def test_cancellation_before_and_after_mutation(self):
        for mutated in (False, True):
            runtime = self.context()
            with self.assertRaises(KeyboardInterrupt):
                with runtime:
                    if mutated:
                        runtime.run('pane', 'focus', 'fixture')
                        self.assertTrue((runtime.root / 'mutation-after').exists())
                    raise KeyboardInterrupt()
            self.assert_stopped(runtime)

    def test_sigterm_during_enter_and_body_reaps_owned_children(self):
        previous = signal.getsignal(signal.SIGTERM)
        for stage in ('server-started', 'body'):
            runtime = self.context()
            original = runtime._event
            def event(name, **details):
                original(name, **details)
                if name == stage:
                    os.kill(os.getpid(), signal.SIGTERM)
            with mock.patch.object(runtime, '_event', event):
                with self.assertRaises(KeyboardInterrupt):
                    with runtime:
                        os.kill(os.getpid(), signal.SIGTERM)
            self.assert_stopped(runtime)
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_child_environment_cannot_redirect_socket_or_home(self):
        with self.context(real=False) as runtime:
            for key, value in (('HERDR_SOCKET_PATH', '/tmp/default.sock'),
                               ('HOME', '/forbidden'), ('HERDR_CONFIG_PATH', '/forbidden')):
                with self.assertRaises(OwnershipError):
                    runtime.execute(['/bin/true'], env=runtime.env | {key: value})
            self.assertEqual(runtime._children, [])

    def test_timeout_retains_both_output_streams_and_reaps_the_child(self):
        runtime = self.context(real=False)
        script = (
            "import sys,time; "
            "print('timeout stdout marker', flush=True); "
            "print('timeout stderr marker', file=sys.stderr, flush=True); "
            "time.sleep(30)"
        )
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            with runtime:
                runtime.execute([sys.executable, "-c", script], timeout=.2)
        error = raised.exception
        self.assertIn("timeout stdout marker", error.stdout)
        self.assertIn("timeout stderr marker", error.stderr)
        self.assertEqual(error.stage, Path(sys.executable).name)
        evidence = Path(error.diagnostic_path)
        self.assertIn("timeout stdout marker", (evidence / "stdout.txt").read_text())
        self.assertIn("timeout stderr marker", (evidence / "stderr.txt").read_text())
        self.assertEqual(json.loads((evidence / "result.json").read_text())["status"], "TIMEOUT")
        self.assert_stopped(runtime)
        self.assertIsNone(runtime._lease)

    def test_nonzero_exit_retains_bounded_diagnostics(self):
        with self.context(real=False) as runtime:
            result = runtime.execute([
                sys.executable, "-c",
                "import sys; sys.stdout.write('x' * 70000 + 'exit stdout marker\\n'); "
                "print('exit stderr marker', file=sys.stderr); raise SystemExit(7)",
            ], check=False)
            self.assertEqual(result.returncode, 7)
            self.assertIn("exit stdout marker", result.stdout)
            self.assertIn("exit stderr marker", result.stderr)
            self.assertLessEqual(len(result.stdout.encode()), 65536)
            evidence = Path(result.diagnostic_path)
            record = json.loads((evidence / "result.json").read_text())
            self.assertEqual(record["status"], "NONZERO")
            self.assertLessEqual((evidence / "stdout.txt").stat().st_size, 65536)
            self.assertLessEqual((evidence / "stderr.txt").stat().st_size, 65536)

    def test_cancellation_retains_child_output_without_leaking_processes(self):
        runtime = self.context(real=False)
        script = (
            "import sys,time; "
            "print('cancel stdout marker', flush=True); "
            "print('cancel stderr marker', file=sys.stderr, flush=True); "
            "time.sleep(30)"
        )
        timer = threading.Timer(1, os.kill, args=(os.getpid(), signal.SIGTERM))
        timer.start()
        with self.assertRaises(KeyboardInterrupt) as raised:
            try:
                with runtime:
                    runtime.execute([sys.executable, "-c", script], timeout=5)
            finally:
                timer.cancel()
        error = raised.exception
        self.assertIn("cancel stdout marker", error.stdout)
        self.assertIn("cancel stderr marker", error.stderr)
        evidence = Path(error.diagnostic_path)
        self.assertEqual(json.loads((evidence / "result.json").read_text())["status"], "CANCELLED")
        self.assert_stopped(runtime)
        self.assertIsNone(runtime._lease)

    def test_timeout_remains_primary_when_cleanup_also_fails(self):
        runtime = self.context(real=False)
        runtime.__enter__()
        original_close = runtime.close
        def failed_close():
            original_close()
            raise OwnershipError("injected cleanup failure")
        try:
            with mock.patch.object(runtime, "close", side_effect=failed_close):
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    runtime.execute([
                        sys.executable, "-c",
                        "import sys,time; print('primary marker', flush=True); time.sleep(30)",
                    ], timeout=.2)
        finally:
            original_close()
        evidence = exception_evidence(raised.exception)
        self.assertIn("TimeoutExpired", evidence["error"])
        self.assertIn("injected cleanup failure", evidence["error"])
        self.assertIn("primary marker", raised.exception.stdout)
        self.assert_stopped(runtime)

    def test_public_diagnostics_redact_control_values_and_secret_assignments(self):
        output = public_diagnostic(
            "use lease-value\ntoken=printed-value\nuseful marker\n",
            {"HERDR_RUNTIME_LEASE_TOKEN": "lease-value"},
        )
        self.assertNotIn("lease-value", output)
        self.assertNotIn("printed-value", output)
        self.assertIn("useful marker", output)

    def test_every_command_revalidates_routing(self):
        runtime = self.context()
        with runtime:
            before = len(self.commands())
            for args in (('pane', 'focus', 'x', '--session', 'default'),
                         ('--remote', 'host'), ('session', 'attach', 'default'),
                         ('api', 'snapshot', '--socket=/tmp/default.sock')):
                with self.assertRaises(OwnershipError):
                    runtime.run(*args)
            runtime.env['HERDR_SOCKET_PATH'] = '/tmp/default.sock'
            with self.assertRaises(OwnershipError):
                runtime.run('pane', 'focus', 'x')
            self.assertEqual(len(self.commands()), before)
        self.assert_stopped(runtime)

    def test_cleanup_observation_failure_stops_child_and_retains_root(self):
        runtime = self.context()
        runtime.__enter__()
        with mock.patch('owned_session.process_table', side_effect=OSError('injected ps')):
            with self.assertRaises(OwnershipError):
                runtime.close()
        self.assertTrue(all(child.poll() is not None for child in runtime._children))
        self.assertTrue(runtime.root.exists())
        # The fixture server reaps its own pane on TERM. Confirm before removing
        # this deliberately retained, test-created root.
        table = process_table()
        self.assertFalse([pid for pid, record in runtime._identities.items() if same_process(record, table.get(pid))])
        shutil.rmtree(runtime.root)

    def test_cleanup_removal_failure_preserves_evidence(self):
        runtime = self.context()
        runtime.__enter__()
        with mock.patch('owned_session.shutil.rmtree', side_effect=OSError('injected remove')):
            with self.assertRaises(OwnershipError):
                runtime.close()
        self.assertTrue(runtime.root.exists())
        self.assertTrue(all(child.poll() is not None for child in runtime._children))
        self.assertEqual(json.loads((runtime._evidence / 'teardown.json').read_text())['status'], 'FAIL')
        shutil.rmtree(runtime.root)

    def test_existing_artifacts_preserved(self):
        sentinel = self.out / 'ownership.json'
        sentinel.write_bytes(b'earlier false evidence\n')
        with self.context(real=False):
            pass
        with self.context(real=False):
            pass
        self.assertEqual(sentinel.read_bytes(), b'earlier false evidence\n')
        self.assertEqual(len(list(self.out.glob('owned-session-*'))), 2)

    def test_reused_pgid_does_not_adopt_a_new_process_incarnation(self):
        runtime = self.context(real=False)
        old = {'pid': 900001, 'ppid': 1, 'pgid': 900001, 'state': 'Ss',
               'start': 'synthetic old start', 'executable': 'owned-fixture'}
        replacement = old | {'start': 'synthetic replacement start',
                             'executable': 'unowned-fixture'}
        runtime._groups.add(old['pgid'])
        runtime._identities[old['pid']] = old
        with mock.patch('owned_session.process_table', return_value={replacement['pid']: replacement}):
            runtime._remember()
        self.assertEqual(runtime._groups, set())
        self.assertIs(runtime._identities[old['pid']], old)
        self.assertFalse(same_process(runtime._identities[old['pid']], replacement))

    def test_inherited_socket_link_tracks_the_resolved_endpoint(self):
        target = self.out / 'endpoint.sock'
        link = self.out / 'inherited.sock'
        link.symlink_to(target)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(target))
        self.addCleanup(listener.close)
        record = {'pid': 123, 'ppid': 1, 'pgid': 123, 'state': 'Ss',
                  'start': 'synthetic socket owner', 'executable': 'nvim'}
        lsof_paths = []
        def lsof(args, **kwargs):
            lsof_paths.append(args[-1])
            return subprocess.CompletedProcess(args, 0, '123\n', '')
        environment = {'HOME': str(self.out), 'NVIM': str(link)}
        with mock.patch('owned_session.process_table', return_value={123: record}), \
             mock.patch('owned_session.subprocess.run', side_effect=lsof):
            before = default_identities(environment)
            old_target = socket_identity(target)
            listener.close()
            target.unlink()
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(target))
            self.addCleanup(listener.close)
            after = default_identities(environment)
        self.assertNotEqual(old_target, socket_identity(target))
        self.assertNotEqual(before['sockets'], after['sockets'])
        self.assertEqual(lsof_paths, [str(target.resolve()), str(target.resolve())])

    def test_exception_evidence_keeps_primary_and_cleanup_failures(self):
        primary = 'synthetic workload failure'
        cleanup = 'synthetic cleanup failure'
        try:
            try:
                raise RuntimeError(primary)
            finally:
                raise OwnershipError(cleanup)
        except OwnershipError as exc:
            evidence = exception_evidence(exc)
        self.assertIn(primary, evidence['error'])
        self.assertIn(cleanup, evidence['error'])
        self.assertEqual([item['type'] for item in evidence['exceptions']],
                         ['OwnershipError', 'RuntimeError'])

    def test_fixture_receipt_excludes_a_separately_supervised_editor(self):
        supervisor = {'pid': 700001, 'ppid': 1, 'pgid': 700001, 'state': 'Ss',
                      'start': 'synthetic supervisor start', 'executable': 'python3'}
        editor = {'pid': 700002, 'ppid': 700001, 'pgid': 700002, 'state': 'Ss',
                  'start': 'synthetic editor start', 'executable': 'nvim'}
        with tempfile.TemporaryDirectory(prefix='hf-', dir='/tmp') as directory:
            receipt = Path(directory) / 'supervisor.json'
            receipt.write_text(json.dumps({'supervisor': supervisor, 'processes': [editor]}))
            receipt.chmod(0o600)
            with mock.patch('owned_session.process_table', return_value={
                    supervisor['pid']: supervisor, editor['pid']: editor}):
                self.assertEqual(fixture_identities({supervisor['pid']: supervisor,
                                                     editor['pid']: editor}),
                                 {supervisor['pid'], editor['pid']})


if __name__ == '__main__':
    unittest.main()
