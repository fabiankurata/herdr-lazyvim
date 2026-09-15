import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import ast
import contextlib
import socket
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from runtime_lease import (LEASE_FIELDS, LEASE_PID, LEASE_START, LEASE_TOKEN, _process_identity,
                           RecordedRun, RuntimeLease, RuntimeLeaseBusy,
                           RuntimeLeaseError, stop_recorded_run)


REPO = HERE.parents[1]

RUN_RECORDED = """
from pathlib import Path
import sys
from runtime_lease import RecordedRun
run = RecordedRun.start(Path(sys.argv[1]), sys.argv[3:], repo=Path(sys.argv[2]))
raise SystemExit(run.finish())
"""
STOP_RECORDED = """
import sys
from runtime_lease import stop_recorded_run
stop_recorded_run(sys.argv[1])
"""


class RuntimeLeaseTests(unittest.TestCase):
    def python_environment(self, environment=None):
        env = dict(os.environ if environment is None else environment)
        env["PYTHONPATH"] = str(HERE) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def child(self, source, *, cwd=REPO, environment=None):
        return subprocess.run([sys.executable, "-c", source], cwd=cwd,
                              env=self.python_environment(environment),
                              text=True, capture_output=True, timeout=10)

    @contextlib.contextmanager
    def lease_worktrees(self):
        """Make two worktrees with one common Git directory, outside the suite lease."""
        with tempfile.TemporaryDirectory() as temporary:
            root, repo, other = Path(temporary), Path(temporary) / "repo", Path(temporary) / "other"
            repo.mkdir()
            def git(*arguments):
                return subprocess.run(["git", *arguments], cwd=repo, text=True, capture_output=True, check=True)
            git("init")
            git("config", "user.email", "runtime-lease@example.test")
            git("config", "user.name", "Runtime Lease Test")
            (repo / "fixture.txt").write_text("fixture\n")
            git("add", "fixture.txt")
            git("commit", "-m", "fixture")
            git("worktree", "add", "--detach", str(other), "HEAD")
            clean = {key: value for key, value in os.environ.items() if key not in LEASE_FIELDS}
            try:
                yield repo, other, clean
            finally:
                git("worktree", "remove", "--force", str(other))

    def test_contention_across_worktrees_refuses_before_child_can_start(self):
        with self.lease_worktrees() as (repo, other, clean):
            with RuntimeLease.acquire_owner(repo=repo, inherited=clean) as lease:
                self.assertTrue(lease.owner)
                result = self.child("from runtime_lease import RuntimeLease; RuntimeLease.acquire()", cwd=other,
                                    environment=clean)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("runtime lease is held", result.stderr)

    def test_descendant_borrows_only_the_verified_holder_identity(self):
        with self.lease_worktrees() as (repo, other, clean):
            with RuntimeLease.acquire_owner(repo=repo, inherited=clean) as lease:
                result = self.child(
                    "from runtime_lease import RuntimeLease; lease=RuntimeLease.acquire(); print(lease.owner)", cwd=other,
                    environment=lease.child_environment(clean),
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False")

    def test_direct_native_discovery_refuses_a_guarded_module_while_contended(self):
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests/native",
             "-p", "test_checkpoint_processes.py", "-v"],
            cwd=REPO, env={key: value for key, value in os.environ.items() if key not in LEASE_FIELDS},
            text=True, capture_output=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit owner wrapper", result.stderr)
        self.assertIn("_FailedTest", result.stdout + result.stderr)

    def test_crashed_owner_releases_kernel_lock_without_removing_lockfile(self):
        with self.lease_worktrees() as (repo, other, clean):
            result = self.child("from runtime_lease import RuntimeLease; RuntimeLease.acquire(); import os; os._exit(0)",
                                cwd=repo, environment=clean)
            self.assertEqual(result.returncode, 0, result.stderr)
            with RuntimeLease.acquire_owner(repo=other, inherited=clean) as lease:
                self.assertTrue(lease.owner)

    def test_stale_or_forged_holder_metadata_fails_closed(self):
        with self.lease_worktrees() as (repo, other, clean):
            with RuntimeLease.acquire_owner(repo=repo, inherited=clean) as lease:
                forged = lease.child_environment(clean)
                forged[LEASE_TOKEN] = "forged-token"
                result = self.child("from runtime_lease import RuntimeLease; RuntimeLease.acquire()", cwd=other, environment=forged)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("stale, released, or forged", result.stderr)
                stale = lease.child_environment(clean)
                stale[LEASE_PID] = "999999"
                result = self.child("from runtime_lease import RuntimeLease; RuntimeLease.acquire()", cwd=other, environment=stale)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("stale, released, or forged", result.stderr)

    def test_released_lease_cannot_be_borrowed_while_holder_lives(self):
        with self.lease_worktrees() as (repo, other, clean):
            lease = RuntimeLease.acquire_owner(repo=repo, inherited=clean)
            environment = lease.child_environment(clean)
            lease.close()
            result = self.child("from runtime_lease import RuntimeLease; RuntimeLease.borrow()", cwd=other,
                                environment=environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale, released, or forged", result.stderr)

    def test_failed_start_releases_its_owner_lease(self):
        with self.lease_worktrees() as (repo, other, clean):
            source = """
from pathlib import Path
import tempfile
from runtime_lease import RecordedRun, RuntimeLease
with tempfile.TemporaryDirectory() as temporary:
    try:
        RecordedRun.start(Path(temporary) / 'run.json', ['/definitely-not-a-command'], repo=Path.cwd())
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError('failed start unexpectedly succeeded')
lease = RuntimeLease.acquire_owner()
print(lease.owner)
lease.close()
"""
            result = self.child(source, cwd=repo, environment=clean)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "True")
            with RuntimeLease.acquire_owner(repo=other, inherited=clean) as lease:
                self.assertTrue(lease.owner)

    def test_stop_only_signals_the_recorded_child_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            try:
                run = RecordedRun.start(Path(temporary) / "run.json", [sys.executable, "-c", "import time; time.sleep(30)"], repo=REPO)
                stop_recorded_run(run.path)
                self.assertEqual(run.finish(), -15)
                self.assertIsNone(unrelated.poll(), "unrelated child was signalled")
                receipt = json.loads(run.path.read_text())
                self.assertEqual(receipt["status"], "STOPPED")
                self.assertEqual(receipt["child"]["pid"], run.process.pid)
                self.assertEqual(receipt["exit_code"], -15)
                self.assertIn("finished_at", receipt)
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=3)

    def test_malformed_disconnected_and_slow_control_clients_do_not_break_valid_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = RecordedRun.start(Path(temporary) / "run.json", [sys.executable, "-c", "import time; time.sleep(30)"], repo=REPO)
            try:
                control = run.record["control"]
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(2); client.connect(control["socket"])
                client.sendall(b'{"run_id":1,"token":2}')
                self.assertIn("refused", client.recv(4096).decode())
                client.close()
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.connect(control["socket"]); client.close()
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(2); client.connect(control["socket"]); time.sleep(.6); client.close()
                stop_recorded_run(run.path, timeout=2)
                self.assertEqual(run.finish(), -15)
            finally:
                if run.process.poll() is None:
                    run._stop_owned()
                    run.finish()

    def test_stop_reaps_known_term_ignoring_descendant_but_not_sibling(self):
        with tempfile.TemporaryDirectory() as temporary:
            child_pid = Path(temporary) / "child.pid"
            leader = (
                "import pathlib,signal,subprocess,sys,time; "
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                "p=subprocess.Popen([sys.executable,'-c',\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"]); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
            )
            sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            run = RecordedRun.start(Path(temporary) / "run.json", [sys.executable, "-c", leader, str(child_pid)], repo=REPO)
            try:
                deadline = time.monotonic() + 3
                while not child_pid.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(child_pid.exists())
                descendant = int(child_pid.read_text())
                stop_recorded_run(run.path, timeout=2)
                self.assertEqual(run.finish(), 0)
                self.assertIsNone(_process_identity(descendant))
                self.assertIsNone(sibling.poll())
            finally:
                if run.process.poll() is None:
                    run._stop_owned()
                    run.finish()
                sibling.terminate()
                sibling.wait(timeout=3)

    def test_stop_reaches_supervisor_while_finish_waits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid = root / "child.pid"
            output = root / "run"
            leader = (
                "import pathlib,signal,subprocess,sys,time; "
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                "p=subprocess.Popen([sys.executable,'-c',\"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\"]); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
            )
            sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            receipt = output / "run.json"
            output.mkdir()
            runner = subprocess.Popen([
                sys.executable, "-c", RUN_RECORDED, str(receipt), str(REPO),
                sys.executable, "-c", leader, str(child_pid),
            ], cwd=REPO, env=self.python_environment(), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not receipt.exists() and time.monotonic() < deadline and runner.poll() is None:
                    time.sleep(.02)
                if not receipt.exists() and runner.poll() is not None:
                    stdout, stderr = runner.communicate(timeout=1)
                    self.fail("recorded runner exited before its receipt:\n" + stdout + stderr)
                self.assertTrue(receipt.exists(), "recorded runner did not publish its receipt")
                while not child_pid.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(child_pid.exists())
                result = subprocess.run(
                    [sys.executable, "-c", STOP_RECORDED, str(receipt)], cwd=REPO,
                    env=self.python_environment(), text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                runner.wait(timeout=5)
                self.assertIsNone(_process_identity(int(child_pid.read_text())))
                self.assertIsNone(sibling.poll())
            finally:
                if runner.poll() is None:
                    runner.terminate()
                    runner.wait(timeout=3)
                runner.stdout.close()
                runner.stderr.close()
                sibling.terminate()
                sibling.wait(timeout=3)

    def test_stop_refuses_reused_or_stale_record_without_a_signal(self):
        with tempfile.TemporaryDirectory() as temporary:
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            receipt = Path(temporary) / "run.json"
            try:
                receipt.write_text(json.dumps({"child": {"pid": unrelated.pid, "ppid": 1, "start": "wrong"}, "group": unrelated.pid}))
                with self.assertRaisesRegex(RuntimeLeaseError, "no live supervisor"):
                    stop_recorded_run(receipt)
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=3)

    def test_helper_refuses_forged_sibling_receipt_without_signalling_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            receipt = Path(temporary) / "forged.json"
            try:
                receipt.write_text(json.dumps({"version": 2, "control": {"socket": "/tmp/no-such-supervisor.sock",
                                                "run_id": "forged", "token": "forged"},
                                          "child": _process_identity(unrelated.pid), "group": unrelated.pid}))
                result = subprocess.run(
                    [sys.executable, "-c", STOP_RECORDED, str(receipt)], cwd=REPO,
                    env=self.python_environment(), text=True, capture_output=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(unrelated.poll(), result.stdout + result.stderr)
            finally:
                unrelated.terminate()
                unrelated.wait(timeout=3)

    def test_every_native_test_with_a_child_operation_requires_the_lease(self):
        test_roots = (REPO / "tests/live", REPO / "tests/native", REPO / "tests/profiles", REPO / "tests/prototypes")
        child_names = {"Popen", "run", "check_output", "call", "fork", "kill", "killpg"}
        guarded = []
        for root in test_roots:
            for path in sorted(root.glob("test_*.py")):
                tree = ast.parse(path.read_text(), filename=str(path))
                has_child_operation = any(isinstance(node, ast.Attribute) and node.attr in child_names
                                          for node in ast.walk(tree))
                if has_child_operation and path.name != "test_runtime_lease.py":
                    guarded.append(str(path.relative_to(REPO)))
                    self.assertIn("require_runtime_lease()", path.read_text(), path)
        self.assertEqual(guarded, ["tests/live/test_fake_model.py", "tests/live/test_owned_session.py",
                                   "tests/live/test_source.py", "tests/native/test_checkpoint_processes.py",
                                   "tests/native/test_clipboard.py", "tests/native/test_remote_ui.py",
                                   "tests/native/test_server_namespaces.py", "tests/native/test_workspace_fixture.py"])


if __name__ == "__main__":
    unittest.main()
