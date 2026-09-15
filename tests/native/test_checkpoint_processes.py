import copy
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
LIVE = HERE.parent / "live"
if str(LIVE) not in sys.path:
    sys.path.insert(0, str(LIVE))
from runtime_lease import require_runtime_lease
require_runtime_lease()

import checkpoint
from workspace_fixture import OwnershipError


STATE = {
    "tabs": [{"tab_id": "tab", "workspace_id": "workspace", "label": "original-label"}],
    "panes": [{"pane_id": "editor", "tab_id": "tab"}],
    "focused_workspace_id": "workspace",
    "focused_tab_id": "prior",
    "focused_pane_id": "prior-pane",
    "layouts": [],
}
TARGET = {
    "socket": "/tmp/parent.sock", "workspace_id": "parent-workspace", "workspace_label": "parent",
    "development_tab_id": "parent:tab", "development_pane_id": "parent:pane",
    "development_terminal_id": "parent-terminal", "alacritty_executable": "/tmp/alacritty",
    "alacritty_pid": 1, "alacritty_start": "test",
}


class Session:
    session = "isolated"
    socket = Path("/tmp/isolated.sock")
    herdr = "/tmp/herdr"
    env = {"HOME": "/tmp/home", "PATH": "/usr/bin", "XDG_CONFIG_HOME": "/tmp/config",
           "XDG_DATA_HOME": "/tmp/data", "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache"}

    def __init__(self):
        self.state = copy.deepcopy(STATE)

    def run(self, *args):
        if args[:2] == ("tab", "focus"):
            self.state["focused_tab_id"] = args[2]
        if args[:2] == ("tab", "rename"):
            for tab in self.state["tabs"]:
                if tab["tab_id"] == args[2]:
                    tab["label"] = args[3]
                    break
        return type("Result", (), {"stdout": json.dumps({"result": {"snapshot": self.state}})})()


class LocalFixture:
    """Minimal parent fixture boundary that launches the real relay in a local PTY."""
    instances = []

    def __init__(self, artifact, target):
        self.artifact = Path(artifact)
        self.fixture_root = Path(tempfile.mkdtemp(prefix="native-fixture-", dir="/tmp"))
        self.fixture = {"tab_id": "parent:fixture", "pane_id": "parent:fixture-pane"}
        self.parent_pid = None
        self.commands = []
        self.master = self.slave = None
        self.parent_output = bytearray()
        self.draining = False
        self.drain_thread = None
        self.parent_reaped = False
        LocalFixture.instances.append(self)

    def __enter__(self):
        self.artifact.mkdir(parents=True, exist_ok=True)
        self.fixture_root.mkdir(parents=True, exist_ok=True)
        self.parent_pid, self.master = pty.fork()
        if self.parent_pid == 0:
            os.execl("/bin/sh", "sh")
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.draining = True
        self.drain_thread = threading.Thread(target=self._drain, daemon=True)
        self.drain_thread.start()
        return self

    def __exit__(self, *unused):
        if self.parent_pid is not None and not self._reap_parent(.5):
            os.kill(self.parent_pid, signal.SIGTERM)
            if not self._reap_parent(.5):
                os.kill(self.parent_pid, signal.SIGKILL)
                self._reap_parent(1)
        self.draining = False
        if self.drain_thread is not None:
            self.drain_thread.join(timeout=1)
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
        for path in sorted(self.fixture_root.glob("**/*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.fixture_root.rmdir()
        return False

    def _drain(self):
        while self.draining:
            if not select.select([self.master], [], [], .05)[0]:
                continue
            try:
                self.parent_output.extend(os.read(self.master, 65536))
            except OSError:
                return

    def _reap_parent(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(self.parent_pid, os.WNOHANG)
            except ChildProcessError:
                self.parent_reaped = True
                return True
            if pid == self.parent_pid:
                self.parent_reaped = True
                return True
            time.sleep(.01)
        return False

    def parent_alive(self):
        try:
            os.kill(self.parent_pid, 0)
            return True
        except ProcessLookupError:
            return False

    def run(self, command):
        self.commands.append(command)
        os.write(self.master, (command + "\r").encode())

    def parent_pty_grid(self):
        rows, columns = struct.unpack("HHHH", fcntl.ioctl(self.master, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        return {"rows": rows, "columns": columns, "tty": "local", "shell_pid": 0}


class CheckpointProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        LocalFixture.instances.clear()

    def test_render_timeout_reaps_actual_viewer_and_relay_before_restoration(self):
        root = Path(self.temp.name) / "checkpoint"
        fake_client = Path(self.temp.name) / "fake_client.py"
        fake_client.write_text('''import json, os, select, sys, tty
snapshot = {"tabs": [{"tab_id": "tab", "workspace_id": "workspace"}],
            "panes": [{"pane_id": "editor", "tab_id": "tab"}],
            "focused_workspace_id": "workspace", "focused_tab_id": "tab",
            "focused_pane_id": "prior-pane", "layouts": []}
if sys.argv[1:] == ["api", "snapshot"]:
    print(json.dumps({"result": {"snapshot": snapshot}}))
    raise SystemExit(0)
tty.setraw(0)
os.write(1, b"\\x1b[6n")
while True:
    select.select([0], [], [], .1)
''')
        restored = []

        def restore():
            fixture = LocalFixture.instances[-1]
            receipt_path = fixture.fixture_root / "viewer-relay.json"
            if not receipt_path.exists():
                self.fail("relay did not publish receipt; parent output=" + repr(bytes(fixture.parent_output)))
            receipt = json.loads(receipt_path.read_text())
            for pid in (receipt["child_pid"], receipt["relay_pid"]):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            self.assertTrue(fixture.parent_alive())
            parent_alive = root / "parent-alive"
            fixture.run("printf parent-alive > " + str(parent_alive))
            original_wait(lambda: parent_alive.read_text() if parent_alive.exists() else None,
                          "fixture parent shell did not survive relay")
            self.assertEqual(parent_alive.read_text(), "parent-alive")
            self.assertTrue((root / "viewer-render.bin").exists())
            restored.append("after-real-cleanup")
            return {"status": "PASS"}

        context = {
            "viewer_kind": "herdr", "native_target_file": "/tmp/private-target.json",
            "isolated_session": "isolated", "isolated_socket": "/tmp/isolated.sock",
            "workspace_id": "workspace", "tab_id": "tab", "editor_pane": "editor",
            "render_marker": "PR00CAP-ABCDEF123456", "viewport_before": {"grid": [24, 80]},
            "restore_viewport": restore, "retention": lambda: {"status": "PASS"},
        }
        original_wait = checkpoint.wait_for

        def short_wait(predicate, label, timeout=5):
            if label == "native checkpoint viewer did not render target text":
                timeout = min(timeout, .25)
            return original_wait(predicate, label, timeout=timeout)

        def fake_viewer_argv(session, context):
            return [str(Path(sys.executable).resolve()), str(fake_client)], str(Path(sys.executable).resolve()), {"HERDR_SOCKET_PATH": str(session.socket)}

        session = Session()
        with mock.patch("checkpoint.load_target", return_value=TARGET), \
             mock.patch("checkpoint.WorkspaceFixture", LocalFixture), \
             mock.patch("checkpoint.viewer_argv", side_effect=fake_viewer_argv), \
             mock.patch("checkpoint.wait_for", side_effect=short_wait):
            with self.assertRaisesRegex(OwnershipError, "did not render target text"):
                checkpoint.capture_checkpoint(session, root.parent, root.name, context)

        self.assertEqual(restored, ["after-real-cleanup"])
        receipt = json.loads((root / "checkpoint.json").read_text())
        self.assertEqual(receipt["status"], "FAIL")
        self.assertIn("did not render target text", receipt["primary_error"])
        self.assertEqual(receipt["viewer_cleanup"], "PASS")
        self.assertEqual(receipt["relay_cleanup"], "PASS")
        self.assertEqual(receipt["render_marker_restore"], "PASS")
        self.assertEqual(session.state["tabs"][0]["label"], "original-label")
        final_relay = json.loads((root / "viewer-relay-final.json").read_text())
        self.assertIn("child_exit", final_relay)
        self.assertFalse(LocalFixture.instances[-1].commands[0].startswith("exec "))
        self.assertTrue(LocalFixture.instances[-1].parent_reaped)
        self.assertIn(b"\x1b[6n", (root / "viewer-render.bin").read_bytes())


if __name__ == "__main__":
    unittest.main()
