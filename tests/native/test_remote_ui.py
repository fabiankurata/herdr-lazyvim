import json
import os
from pathlib import Path
import pty
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import remote_ui


class RemoteUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def wait(self, predicate, label):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail(label)

    def driver(self, child, *, recording=True):
        root = Path(self.temp.name)
        script, receipt, transcript, recording_path = root / "driver.py", root / "receipt.json", root / "terminal.bin", root / "recording"
        script.write_text(remote_ui.DRIVER)
        remote_ui.write_recording_mode(recording_path, recording)
        return subprocess.Popen([sys.executable, str(script), "--receipt", str(receipt), "--transcript", str(transcript),
                                 "--recording-path", str(recording_path), "--rows", "24", "--columns", "80", "--", sys.executable, str(child)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE), receipt, transcript

    def source_lane(self, *, parent_columns=80, fail_key=False):
        """A local PTY fixture: its inputs and capture are simulated, never native evidence."""
        test = self

        class LocalPtyFixture:
            instances = []

            def __init__(self, artifact, target):
                self.artifact = Path(artifact)
                self.fixture_root = None
                self.retention_reason = None
                self.master = self.proc = None
                self.events = []
                self.output = bytearray()
                self.parent = {"rows": 24, "columns": parent_columns, "simulated": True}
                type(self).instances.append(self)

            def __enter__(self):
                self.artifact.mkdir()
                self.fixture_root = Path(tempfile.mkdtemp(prefix="remote-ui-pty-", dir="/tmp"))
                return self

            def run(self, command):
                if parent_columns != 80:
                    command = command.replace("--columns " + str(parent_columns), "--columns 80")
                self.master, slave = pty.openpty()
                self.proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=slave, stdout=slave, stderr=slave,
                                             start_new_session=True)
                os.close(slave)
                self.events.append({"operation": "simulated-pty-launch"})
                def drain():
                    while True:
                        try:
                            data = os.read(self.master, 65536)
                        except OSError:
                            return
                        if not data:
                            return
                        self.output.extend(data)
                self.reader = threading.Thread(target=drain, daemon=True)
                self.reader.start()

            def parent_pty_grid(self):
                return self.parent

            def fixture_focus(self):
                self.events.append({"operation": "simulated-focus"})

            def key(self, kind, text=None):
                self.events.append({"operation": "simulated-key", "kind": kind, "text": text})
                if fail_key:
                    raise remote_ui.OwnershipError("simulated input failure")
                data = text.encode("ascii") if kind == "text" else b"\x13"
                os.write(self.master, data)

            def capture(self, **kwargs):
                self.events.append({"operation": "simulated-capture", "native_evidence": False, **kwargs})
                return {"status": "SIMULATED", "native_evidence": False}

            def retain_runtime(self, reason):
                self.retention_reason = reason
                self.events.append({"operation": "simulated-runtime-retained", "reason": reason})
                (self.artifact / "runtime-retained.json").write_text(json.dumps({
                    "reason": reason, "runtime": str(self.fixture_root),
                }))

            def __exit__(self, exc_type, exc, traceback):
                try:
                    ready = self.artifact.parent / "remote-ui-ready.json"
                    if ready.exists():
                        identities = json.loads(ready.read_text())
                    transcript = self.artifact.parent / "remote-ui-terminal.bin"
                    if self.proc is not None:
                        self.proc.wait(timeout=3)
                    if ready.exists():
                        for name in ("server", "lsp", "driver", "ui"):
                            with test.assertRaises(ProcessLookupError, msg=name + " survived fixture cleanup"):
                                os.kill(identities[name]["pid"], 0)
                    if ready.exists():
                        test.assertTrue(transcript.exists(), "cleanup must retain transcript before fixture teardown")
                    (self.artifact / "simulated-operations.json").write_text(json.dumps(self.events))
                finally:
                    if self.proc is not None and self.proc.poll() is None:
                        self.proc.terminate()
                        self.proc.wait(timeout=3)
                    if self.master is not None:
                        os.close(self.master)
                    if self.fixture_root is not None and self.retention_reason is None:
                        shutil.rmtree(self.fixture_root)

        return LocalPtyFixture

    def source_target(self):
        path = Path(self.temp.name) / "target.json"
        path.write_text(json.dumps({
            "socket": "/tmp/remote-ui-test.sock", "workspace_id": "workspace", "workspace_label": "test",
            "development_tab_id": "workspace:tab", "development_pane_id": "workspace:pane",
            "development_terminal_id": "terminal", "alacritty_executable": "/tmp/alacritty",
            "alacritty_pid": 1, "alacritty_start": "test",
        }))
        path.chmod(0o600)
        return path

    def run_source_lane(self, *, profile="plain", lane_options=None, **fixture_options):
        artifact = Path(self.temp.name) / "artifact"
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE.parents[1], text=True).strip()
        return remote_ui.run_lane(HERE.parents[1], revision, artifact, str(self.source_target()), profile=profile,
                                  fixture_factory=self.source_lane(**fixture_options), **(lane_options or {})), artifact

    def test_context_receipt_precedes_any_fixture_or_input(self):
        root = Path(self.temp.name) / "artifact"; root.mkdir()
        session = type("Session", (), {"session": "owned", "socket": Path("/tmp/owned.sock")})()
        context = remote_ui.remote_context("a" * 40, "/tmp/private-target.json", session)
        path = remote_ui.write_context(root, context)
        self.assertEqual(json.loads(path.read_text()), context)
        self.assertEqual(context["lazyvim"], "PENDING")
        self.assertEqual(context["clipboard"], "PENDING")

    def test_clipboard_protocol_uses_line_json_and_reaps_after_terminate(self):
        helper = Path(self.temp.name) / "fake-clipboard.py"
        helper.write_text("""import json, sys
print(json.dumps({'event':'ready','status':'PASS'}), flush=True)
for line in sys.stdin:
 command = json.loads(line)
 op = command['op']
 if op == 'seed': receipt = {'status':'PASS','synthetic':'seeded'}
 elif op == 'check': receipt = {'status':'PASS','synthetic':'matched'}
 else: receipt = {'status':'PASS','restoration':'restored'}
 print(json.dumps(receipt), flush=True)
 if op == 'terminate': break
""")
        client = remote_ui.ClipboardProtocol([sys.executable, str(helper)])
        self.assertEqual(client.command({"op": "seed", "nonce": "synthetic"})["synthetic"], "seeded")
        self.assertEqual(client.command({"op": "check", "expected": "synthetic\\n"})["synthetic"], "matched")
        self.assertEqual(client.close()["restoration"], "restored")
        self.assertIsNotNone(client.process.returncode)

    def test_clipboard_receipt_requires_an_explicit_verdict(self):
        self.assertEqual(remote_ui.clipboard_receipt_status({"status": "UNVERIFIED"}), "UNVERIFIED")
        with self.assertRaises(remote_ui.OwnershipError):
            remote_ui.clipboard_receipt_status({"status": "SIMULATED"})

    def test_clipboard_exercise_uses_native_register_text_with_a_simulated_bridge(self):
        source, runtime = Path(self.temp.name) / "source", Path(self.temp.name) / "runtime"
        (source / "tests/native").mkdir(parents=True)
        (source / "tests/native/clipboard.swift").write_text("// exact archived source in the real lane\n")
        runtime.mkdir()
        original = {"win": 10, "buf": 20, "cursor": [1, 0], "dirty": True,
                    "lines": ["remote ui unsaved"], "lsp": 1}
        scratch = {"win": 11, "buf": 21}
        paste_nonce = "pr00-clipboard-paste-" + "a" * 32
        yank_nonce = "pr00-clipboard-yank-" + "b" * 32
        calls = []

        class Fixture:
            def fixture_focus(self):
                calls.append(("focus",))
            def key(self, kind, text):
                calls.append(("key", kind, text))

        class Client:
            def __init__(self, command):
                self.command_line = command
                self.commands = []
            def command(self, value):
                self.commands.append(value)
                return {"status": "PASS", "synthetic": "matched" if value["op"] == "check" else "seeded"}
            def close(self):
                return {"status": "PASS", "restoration": "restored"}

        clients = []
        def client_factory(command):
            client = Client(command); clients.append(client); return client
        def fake_remote(_nvim, _socket, lua, **_kwargs):
            if lua == remote_ui.clipboard_lua_state():
                return original
            if "belowright new" in lua:
                return scratch
            if "pr00_fake_clipboard=" in lua:
                return True
            if "mode=vim.api.nvim_get_mode" in lua:
                return {"mode": "n", "win": 11, "buf": 21, "lines": [""]}
            if "local lines=vim.api.nvim_buf_get_lines" in lua:
                return True
            if "buf_set_lines" in lua:
                return True
            if "get_mode().mode" in lua:
                return "n"
            if "vim.g.pr00_fake_clipboard==" in lua:
                return True
            if "local s=" in lua:
                return original
            raise AssertionError(lua)

        with mock.patch.object(remote_ui.shutil, "which", return_value="/fake/swiftc"), \
             mock.patch.object(remote_ui.subprocess, "run"), \
             mock.patch.object(remote_ui, "remote_expression", side_effect=fake_remote), \
             mock.patch.object(remote_ui.uuid, "uuid4", side_effect=[type("U", (), {"hex": "a" * 32})(), type("U", (), {"hex": "b" * 32})()]):
            result = remote_ui.run_clipboard_exercise(source, runtime, Fixture(), "nvim", "/tmp/socket", {"PATH": "/bin"},
                                                       client_factory=client_factory, bridge="SIMULATED")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["bridge"], "SIMULATED")
        self.assertEqual(calls, [("focus",), ("key", "text", '"+p'), ("focus",), ("key", "text", 'gg"+yy')])
        self.assertEqual(clients[0].commands, [{"op": "seed", "nonce": paste_nonce},
                                                 {"op": "check", "expected": yank_nonce + "\n"}])
        self.assertEqual(clients[0].command_line[1:3], ["--general", "--idle-timeout-ms"])

    def test_bidirectional_driver_forwards_input_and_reaps_on_readiness_failure(self):
        child = Path(self.temp.name) / "child.py"
        child.write_text("import os,time\ndata=os.read(0,2)\nos.write(1,b'input:'+data)\nwhile True: time.sleep(.1)\n")
        driver, receipt_path, transcript = self.driver(child)
        early = self.wait(lambda: json.loads(receipt_path.read_text()) if receipt_path.exists() else None, "missing early receipt")
        self.assertFalse(early["rendered"])
        driver.stdin.write(b"X\n"); driver.stdin.flush()
        self.wait(lambda: json.loads(receipt_path.read_text()).get("rendered"), "driver did not forward input")
        remote_ui.stop_owned(remote_ui.exact_identity(early["ui"]))
        driver.communicate(timeout=3)
        self.assertEqual(driver.returncode, 0)
        self.assertIn(b"input:X", transcript.read_bytes())
        for identity in (early["ui"], early["driver"]):
            with self.assertRaises(ProcessLookupError):
                os.kill(identity["pid"], 0)

    def test_driver_does_not_persist_a_suppressed_foreign_clipboard_sentinel(self):
        sentinel = "FOREIGN_CLIPBOARD_SENTINEL_7f3c"
        child = Path(self.temp.name) / "child.py"
        child.write_text("import os,time\ntime.sleep(.2)\nos.write(1," + repr(sentinel.encode()) + ")\nwhile True: time.sleep(.1)\n")
        driver, receipt_path, transcript = self.driver(child, recording=False)
        early = self.wait(lambda: json.loads(receipt_path.read_text()) if receipt_path.exists() else None, "missing driver receipt")
        time.sleep(.3)
        self.wait(lambda: json.loads(receipt_path.read_text()).get("rendered"), "driver did not observe child output")
        remote_ui.stop_owned(remote_ui.exact_identity(early["ui"]))
        driver.communicate(timeout=3)
        self.assertNotIn(sentinel.encode(), transcript.read_bytes())

    def test_driver_raw_tty_forwards_printable_bytes_and_restores_parent_termios(self):
        root = Path(self.temp.name)
        child = root / "child.py"
        child.write_text("import os,time,tty\ntty.setraw(0)\nos.write(1,b'ready')\ndata=os.read(0,1)\nos.write(1,b'input:'+data)\nwhile True: time.sleep(.1)\n")
        script, receipt, transcript, usable, recording = root / "driver.py", root / "receipt.json", root / "terminal.bin", root / "parent-usable", root / "recording"
        script.write_text(remote_ui.DRIVER)
        remote_ui.write_recording_mode(recording, True)
        command = remote_ui.shell_command([sys.executable, script, "--receipt", receipt, "--transcript", transcript,
                                            "--recording-path", recording, "--rows", "24", "--columns", "80", "--", sys.executable, child])
        master, slave = pty.openpty()
        slave_name = os.ttyname(slave)
        before = termios.tcgetattr(slave)
        parent = subprocess.Popen(["/bin/sh", "-c", command + "; printf usable > " + str(usable)],
                                  stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        self.addCleanup(lambda: parent.poll() is None and parent.kill())
        try:
            early = self.wait(lambda: json.loads(receipt.read_text()) if receipt.exists() else None, "missing TTY receipt")
            self.wait(lambda: not termios.tcgetattr(slave)[3] & termios.ICANON, "driver did not enter raw parent TTY mode")
            deadline, output = time.monotonic() + 3, bytearray()
            while time.monotonic() < deadline and b"ready" not in output:
                readable, _, _ = select.select([master], [], [], .05)
                if readable:
                    output.extend(os.read(master, 65536))
            self.assertIn(b"ready", output)
            os.write(master, b"X")
            while time.monotonic() < deadline and b"input:X" not in output:
                readable, _, _ = select.select([master], [], [], .05)
                if readable:
                    output.extend(os.read(master, 65536))
            self.assertIn(b"input:X", output)
            remote_ui.stop_owned(remote_ui.exact_identity(early["ui"]))
            parent.wait(timeout=3)
            self.assertEqual(usable.read_text(), "usable")
            restored = os.open(slave_name, os.O_RDWR | os.O_NOCTTY)
            try:
                self.assertEqual(termios.tcgetattr(restored), before)
            finally:
                os.close(restored)
            self.assertIn(b"input:X", transcript.read_bytes())
            for identity in (early["ui"], early["driver"]):
                with self.assertRaises(ProcessLookupError):
                    os.kill(identity["pid"], 0)
        finally:
            os.close(master)
            os.close(slave)

    def test_driver_keeps_parent_shell_alive_after_owned_ui_stops(self):
        root = Path(self.temp.name); child = root / "child.py"; child.write_text("import time\nwhile True: time.sleep(.1)\n")
        script, receipt, transcript, alive, recording = root / "driver.py", root / "receipt.json", root / "terminal.bin", root / "parent-alive", root / "recording"
        script.write_text(remote_ui.DRIVER)
        remote_ui.write_recording_mode(recording, True)
        command = " ".join([sys.executable, str(script), "--receipt", str(receipt), "--transcript", str(transcript), "--recording-path", str(recording), "--rows", "24", "--columns", "80", "--", sys.executable, str(child)])
        parent = subprocess.Popen(["/bin/sh", "-c", command + "; printf alive > " + str(alive)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        early = self.wait(lambda: json.loads(receipt.read_text()) if receipt.exists() else None, "missing parent receipt")
        os.kill(early["driver"]["pid"], signal.SIGTERM)
        parent.communicate(timeout=3)
        self.assertEqual(alive.read_text(), "alive")

    def test_readiness_requires_one_dirty_lsp_attached_ui_at_parent_grid(self):
        parent = {"rows": 24, "columns": 80}
        good = {"dirty": True, "lines": ["remote ui unsaved"], "lsp": 1, "uis": [{}], "grid": [24, 80]}
        self.assertTrue(remote_ui.ready_state(good, parent))
        for changed in (good | {"uis": []}, good | {"dirty": False}, good | {"lsp": 0}, good | {"grid": [25, 80]}):
            self.assertFalse(remote_ui.ready_state(changed, parent))

    def test_plain_profile_uses_the_native_space_mapleader_and_protocol_lsp(self):
        source = Path(remote_ui.__file__).read_text()
        self.assertIn("vim.g.mapleader=' '", source)
        self.assertIn('tests/prototypes/fake_lsp.py', source)
        self.assertIn('profile_run.profile_init', source)

    def test_fixture_cleanup_reaps_direct_server_and_retains_transcript(self):
        root = Path(self.temp.name)
        transcript = root / "runtime-transcript.bin"
        transcript.write_bytes(b"owned terminal output")
        server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: server.poll() is None and server.kill())
        time.sleep(.05)
        identity = self.wait(lambda: remote_ui.process_identity(server.pid), "server identity missing")
        receipt = {"status": "PASS"}
        with remote_ui.fixture_cleanup(lambda: (None, server, identity, None, None, None, None, transcript), root, receipt):
            pass
        self.assertEqual(server.returncode, -signal.SIGTERM)
        with self.assertRaises(ProcessLookupError):
            os.kill(identity["pid"], 0)
        self.assertEqual((root / "remote-ui-terminal.bin").read_bytes(), b"owned terminal output")

    def test_fixture_cleanup_kills_and_reaps_term_ignoring_direct_server(self):
        root = Path(self.temp.name)
        transcript = root / "runtime-transcript.bin"
        transcript.write_bytes(b"owned terminal output")
        server = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
                                  start_new_session=True)
        def reap_server():
            if server.poll() is None:
                server.kill()
            server.wait()
        self.addCleanup(reap_server)
        time.sleep(.05)
        identity = self.wait(lambda: remote_ui.process_identity(server.pid), "server identity missing")
        receipt = {"status": "PASS"}
        with remote_ui.fixture_cleanup(lambda: (None, server, identity, None, None, None, None, transcript), root, receipt):
            pass
        self.assertEqual(server.returncode, -signal.SIGKILL)
        with self.assertRaises(ProcessLookupError):
            os.kill(identity["pid"], 0)
        self.assertEqual((root / "remote-ui-terminal.bin").read_bytes(), b"owned terminal output")

    def test_fixture_cleanup_marks_cleanup_failure_as_failure(self):
        receipt = {"status": "PASS"}
        identity = {"pid": 1, "start": "owned", "executable": "test"}
        with mock.patch.object(remote_ui, "stop_owned", side_effect=remote_ui.OwnershipError("retained")):
            with self.assertRaises(remote_ui.OwnershipError):
                retained = type("RetainedFixture", (), {"reason": None,
                            "retain_runtime": lambda self, reason: setattr(self, "reason", reason)})()
                with remote_ui.fixture_cleanup(lambda: (retained, None, None, None, None, identity, None, None), Path(self.temp.name), receipt):
                    pass
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["cleanup_errors"], ["UI: retained"])
        self.assertIn("UI: retained", retained.reason)

    def test_fixture_cleanup_retains_runtime_without_signaling_changed_server(self):
        root = Path(self.temp.name)
        transcript = root / "runtime-transcript.bin"
        transcript.write_bytes(b"owned terminal output")
        server = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
                                  start_new_session=True)
        def reap_server():
            if server.poll() is None:
                server.kill()
            server.wait()
        self.addCleanup(reap_server)
        time.sleep(.05)
        current = self.wait(lambda: remote_ui.process_identity(server.pid), "server identity missing")
        changed = current | {"executable": current["executable"] + ".changed"}
        retained = type("RetainedFixture", (), {"reason": None,
                    "retain_runtime": lambda self, reason: setattr(self, "reason", reason)})()
        receipt = {"status": "PASS"}
        with self.assertRaises(remote_ui.OwnershipError):
            with remote_ui.fixture_cleanup(lambda: (retained, server, changed, None, None, None, None, transcript), root, receipt):
                pass
        self.assertIsNone(server.poll())
        self.assertEqual(remote_ui.process_identity(server.pid), current)
        self.assertIn("identity changed", retained.reason)
        self.assertEqual((root / "remote-ui-terminal.bin").read_bytes(), b"owned terminal output")

    def test_run_lane_uses_archived_source_with_real_nvim_lsp_and_simulated_pty(self):
        receipt, artifact = self.run_source_lane()
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["provenance"]["mode"], "clean-checkout")
        self.assertEqual(receipt["detached"]["lines"], ["remote ui unsaved"])
        self.assertEqual(receipt["detached"]["lsp"], 1)
        self.assertTrue((artifact / "remote-ui-terminal.bin").exists())
        events = json.loads(next((artifact / "fixture").glob("simulated-operations.json")).read_text())
        self.assertTrue(any(event["operation"] == "simulated-capture" and not event["native_evidence"] for event in events))
        self.assertEqual([event["kind"] for event in events if event["operation"] == "simulated-key"],
                         ["text", "text", "cmd-enter", "text", "text", "ctrl-s"])

    def test_run_lane_uses_archived_lazyvim_profile_with_simulated_pty(self):
        receipt, artifact = self.run_source_lane(profile="lazy")
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["profile"], "lazy")
        ready = json.loads((artifact / "remote-ui-ready.json").read_text())
        self.assertTrue(ready["state"]["lazy_imported"])
        self.assertTrue(ready["state"]["lazy_config"])
        self.assertTrue(ready["dependencies"])
        self.assertTrue((artifact / "lazy-dependencies.json").exists())
        self.assertTrue((artifact / "remote-ui-terminal.bin").exists())

    def test_run_lane_clipboard_uses_fake_provider_and_protocol_bridge(self):
        clients = []
        class Client:
            def __init__(self, command):
                self.command_line, self.commands = command, []
                clients.append(self)
            def command(self, value):
                self.commands.append(value)
                return {"status": "PASS", "synthetic": "matched" if value["op"] == "check" else "seeded"}
            def close(self):
                return {"status": "PASS", "restoration": "restored"}
        receipt, artifact = self.run_source_lane(lane_options={"clipboard": True, "clipboard_bridge": "SIMULATED",
                                                                 "clipboard_client_factory": Client})
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["clipboard"]["status"], "PASS")
        self.assertEqual(receipt["clipboard"]["bridge"], "SIMULATED")
        self.assertEqual(receipt["clipboard"]["restoration"], "PASS")
        self.assertEqual(clients[0].command_line[1:3], ["--general", "--idle-timeout-ms"])
        self.assertEqual([command["op"] for command in clients[0].commands], ["seed", "check"])
        events = json.loads(next((artifact / "fixture").glob("simulated-operations.json")).read_text())
        self.assertEqual([event["text"] for event in events if event["operation"] == "simulated-key"][:2],
                         ['"+p', 'gg"+yy'])

    def test_clipboard_failure_keeps_primary_and_restoration_receipts(self):
        class Client:
            def __init__(self, _command):
                pass
            def command(self, value):
                if value["op"] != "seed":
                    raise AssertionError("clipboard input should fail before yank")
                return {"status": "PASS", "synthetic": "seeded"}
            def close(self):
                return {"status": "UNVERIFIED", "reason": "clipboard-ownership-changed"}
        factory = self.source_lane(fail_key=True)
        artifact = Path(self.temp.name) / "artifact"
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE.parents[1], text=True).strip()
        with self.assertRaises(remote_ui.OwnershipError):
            remote_ui.run_lane(HERE.parents[1], revision, artifact, str(self.source_target()), clipboard=True,
                               clipboard_bridge="SIMULATED", clipboard_client_factory=Client, fixture_factory=factory)
        result = json.loads((artifact / "result.json").read_text())
        self.assertIn("simulated input failure", result["primary_error"])
        self.assertEqual(result["clipboard"]["status"], "UNVERIFIED")
        self.assertEqual(result["clipboard"]["restoration"], "UNVERIFIED")
        self.assertEqual(result["clipboard"]["restoration_reason"], "clipboard-ownership-changed")

    def test_run_lane_readiness_failure_sends_no_simulated_input_and_retains_cleanup_evidence(self):
        factory = self.source_lane(parent_columns=79)
        artifact = Path(self.temp.name) / "artifact"
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE.parents[1], text=True).strip()
        with self.assertRaises(remote_ui.OwnershipError):
            remote_ui.run_lane(HERE.parents[1], revision, artifact, str(self.source_target()), fixture_factory=factory)
        events = factory.instances[0].events
        self.assertFalse(any(event["operation"] == "simulated-key" for event in events))
        self.assertTrue((artifact / "remote-ui-terminal.bin").exists())
        result = json.loads((artifact / "result.json").read_text())
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("attach readiness", result["primary_error"])

    def test_run_lane_input_failure_stops_before_later_simulated_input_or_capture(self):
        factory = self.source_lane(fail_key=True)
        artifact = Path(self.temp.name) / "artifact"
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE.parents[1], text=True).strip()
        with self.assertRaises(remote_ui.OwnershipError):
            remote_ui.run_lane(HERE.parents[1], revision, artifact, str(self.source_target()), fixture_factory=factory)
        events = factory.instances[0].events
        self.assertEqual([event["kind"] for event in events if event["operation"] == "simulated-key"], ["text"])
        self.assertFalse(any(event["operation"] == "simulated-capture" for event in events))
        self.assertTrue((artifact / "remote-ui-terminal.bin").exists())
        result = json.loads((artifact / "result.json").read_text())
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("simulated input failure", result["primary_error"])


if __name__ == "__main__":
    unittest.main()
