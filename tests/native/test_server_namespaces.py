import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
LIVE = HERE.parent / "live"
if str(LIVE) not in sys.path:
    sys.path.insert(0, str(LIVE))
from runtime_lease import require_runtime_lease
require_runtime_lease()
import server_namespaces as namespaces


class NamespaceTests(unittest.TestCase):
    def lane_parts(self, root, mode="success"):
        root = Path(root)
        cli = root / "fake-cli"
        cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"MODE = {mode!r}\n"
            "session = sys.argv[sys.argv.index('--session') + 1]\n"
            "if MODE == 'failure': raise SystemExit(9)\n"
            "suffix = session if MODE == 'mismatch' else ''\n"
            "print(json.dumps({'result': {'snapshot': {'workspaces': [{'workspace_id': 'w' + suffix}], 'tabs': [{'tab_id': 't' + suffix}], 'panes': [{'pane_id': 'p' + suffix}]}}}))\n"
        )
        cli.chmod(0o700)
        events = []

        class Session:
            closed = []
            count = 0

            def __init__(self, artifact):
                number = Session.count
                Session.count += 1
                self.root = root / f"session-{number}"
                self.root.mkdir()
                self.session = f"session-{number}"
                self.socket = root / f"socket-{number}"
                self.herdr = cli
                self.env = dict(os.environ)

            def __enter__(self):
                events.append(f"enter:{self.session}")
                return self

            def __exit__(self, *unused):
                Session.closed.append(self.session)
                events.append(f"exit:{self.session}")

            def run(self, *unused):
                return type("Result", (), {"stdout": json.dumps({"result": {"workspace": {"workspace_id": "w"}, "root_pane": {"pane_id": "p"}}})})()

        class Fixture:
            closed = []
            captures = []
            transcripts = []
            processes = []
            roots = []

            def __init__(self, artifact, target):
                self.fixture_root = Path(artifact) / "runtime"
                self.fixture_root.mkdir(parents=True)
                Fixture.roots.append(self.fixture_root)

            def __enter__(self):
                events.append("enter:fixture")
                return self

            def __exit__(self, *unused):
                for process in Fixture.processes:
                    try:
                        output, error = process.communicate(timeout=.5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        output, error = process.communicate(timeout=.5)
                    Fixture.transcripts.append(output + error)
                Fixture.closed.append(True)
                events.append("exit:fixture")

            def run(self, command):
                events.append("renderer")
                Fixture.processes.append(subprocess.Popen(command, shell=True, cwd=self.fixture_root,
                                                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE))

            def parent_pty_grid(self):
                return {"rows": 24, "columns": 80}

            def capture(self, name, *, viewport_grid):
                events.append("capture")
                Fixture.captures.append((name, viewport_grid))
                return {"status": "SIMULATED"}

        target = root / "target.json"
        target.write_text(json.dumps({
            "socket": "/tmp/s", "workspace_id": "w", "workspace_label": "x",
            "development_tab_id": "w:t", "development_pane_id": "w:p",
            "development_terminal_id": "x", "alacritty_executable": "/tmp/a",
            "alacritty_pid": 1, "alacritty_start": "x",
        }))
        target.chmod(0o600)
        return Session, Fixture, target, events

    def execute_lane(self, root, mode):
        Session, Fixture, target, events = self.lane_parts(root, mode)
        repo = Path(root) / "checked-source"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(HERE.parents[1]), str(repo)], check=True)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        result = namespaces.run_lane(repo, revision, Path(root) / "artifact", target,
                                     session_factory=Session, fixture_factory=Fixture,
                                     renderer_timeout=.5 if mode == "failure" else 3)
        return result, Session, Fixture, events

    def test_run_lane_captures_after_fresh_validated_renderer_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            result, Session, Fixture, events = self.execute_lane(root, "success")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["evidence_kind"], "SIMULATED")
            self.assertEqual(len(Fixture.captures), 1)
            self.assertLess(events.index("renderer"), events.index("capture"))
            self.assertEqual(len(Session.closed), 2)
            self.assertEqual(len(Fixture.closed), 1)
            self.assertTrue(Path(result["receipt"]).is_file())
            self.assertEqual(result["renderer_output"], "session=session-0 socket=" + str(Path(root) / "socket-0") + " workspaces=['w'] tabs=['t'] panes=['p']\nsession=session-1 socket=" + str(Path(root) / "socket-1") + " workspaces=['w'] tabs=['t'] panes=['p']")
            self.assertEqual((Fixture.roots[0] / "namespace-request.json").stat().st_mode & 0o777, 0o600)
            self.assertFalse((Fixture.roots[0] / "namespace-receipt.json.tmp").exists())
            self.assertEqual(json.loads((Path(root) / "artifact" / "result.json").read_text())["status"], "PASS")

    def test_run_lane_renderer_failure_captures_nothing_and_closes_contexts(self):
        with tempfile.TemporaryDirectory() as root:
            result, Session, Fixture, events = self.execute_lane(root, "failure")
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(Fixture.captures, [])
            self.assertEqual(len(Session.closed), 2)
            self.assertEqual(len(Fixture.closed), 1)
            self.assertLess(events.index("exit:fixture"), events.index("exit:session-1"))
            self.assertTrue(Fixture.transcripts[0])
            self.assertTrue(all(process.poll() is not None for process in Fixture.processes))
            self.assertIn("renderer completion receipt timed out", result["error"])
            self.assertEqual(json.loads((Path(root) / "artifact" / "result.json").read_text())["status"], "FAIL")

    def test_run_lane_authority_mismatch_captures_nothing_and_closes_contexts(self):
        with tempfile.TemporaryDirectory() as root:
            result, Session, Fixture, events = self.execute_lane(root, "mismatch")
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(Fixture.captures, [])
            self.assertEqual(len(Session.closed), 2)
            self.assertEqual(len(Fixture.closed), 1)
            self.assertIn("synthetic local IDs did not overlap", result["error"])
            self.assertEqual(json.loads((Path(root) / "artifact" / "result.json").read_text())["status"], "FAIL")

    def test_overlap_requires_distinct_owned_authorities(self):
        left = {"session": "left", "socket": "/tmp/left", "workspaces": ["w"], "tabs": ["w:t"], "panes": ["w:p"]}
        right = {"session": "right", "socket": "/tmp/right", "workspaces": ["w"], "tabs": ["w:t"], "panes": ["w:p"]}
        result = namespaces.compare(left, right)
        self.assertEqual(result["overlap"]["panes"], ["w:p"])
        with self.assertRaises(namespaces.NamespaceError):
            namespaces.compare(left, left)

    def test_observe_uses_each_session_authority(self):
        session = type("S", (), {"session": "isolated", "socket": "/tmp/isolated", "env": {}})()
        output = {"result": {"snapshot": {"workspaces": [{"workspace_id": "w"}], "tabs": [{"tab_id": "t"}], "panes": [{"pane_id": "p"}]}}}
        with mock.patch("server_namespaces.subprocess.run", return_value=type("R", (), {"stdout": json.dumps(output)})()) as run:
            self.assertEqual(namespaces.observe(session, "/tmp/fake")["session"], "isolated")
            self.assertEqual(run.call_args.args[0][:3], ["/tmp/fake", "--session", "isolated"])

    def test_renderer_reads_two_fresh_snapshots(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cli = root / "fake-cli"
            cli.write_text("#!/bin/sh\nprintf '%s\\n' '{\"result\":{\"snapshot\":{\"workspaces\":[{\"workspace_id\":\"w\"}],\"tabs\":[{\"tab_id\":\"t\"}],\"panes\":[{\"pane_id\":\"p\"}]}}}'\n")
            cli.chmod(0o700)
            request = root / "request.json"
            request.write_text(json.dumps({"servers": [{"cli": str(cli), "session": "a", "socket": "a", "env": {}}, {"cli": str(cli), "session": "b", "socket": "b", "env": {}}]}))
            script, receipt = root / "renderer.py", root / "receipt.json"
            script.write_text(namespaces.RENDERER)
            subprocess.run([sys.executable, script, request, receipt], check=True, capture_output=True, text=True)
            self.assertEqual(len(json.loads(receipt.read_text())["servers"]), 2)

    def test_compare_rejects_renderer_authority_or_overlap_mismatch(self):
        with self.assertRaises(namespaces.NamespaceError):
            namespaces.compare({"session": "a", "socket": "a", "workspaces": ["w"], "tabs": ["t"], "panes": ["p"]}, {"session": "b", "socket": "b", "workspaces": [], "tabs": [], "panes": []})

    def test_renderer_fails_when_cli_fails(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            script, request = root / "renderer.py", root / "request.json"
            script.write_text(namespaces.RENDERER)
            request.write_text(json.dumps({"servers": [{"cli": "/bin/false", "session": "a", "socket": "a", "env": {}}]}))
            self.assertNotEqual(subprocess.run([sys.executable, script, request, root / "receipt.json"], capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
