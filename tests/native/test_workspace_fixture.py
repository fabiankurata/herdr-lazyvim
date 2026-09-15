import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import copy
from types import SimpleNamespace
import subprocess
import sys
import time
import importlib.util

_NATIVE_RUN_SPEC = importlib.util.spec_from_file_location(
    "pr00_native_run", Path(__file__).with_name("run.py"))
_native_run = importlib.util.module_from_spec(_NATIVE_RUN_SPEC)
_NATIVE_RUN_SPEC.loader.exec_module(_native_run)
keylog_records = _native_run.keylog_records
result_exit_code = _native_run.result_exit_code
from workspace_fixture import (OwnershipError, UNIX_SOCKET_PATH_MAX, WorkspaceFixture,
                               calibrated_capture_plan, fixture_layout, load_target, nvim_socket_path,
                               parse_capture_calibration, process_identity, select_window)
from checkpoint import capture_checkpoint, checkpoint_context, stop_viewer, viewer_argv
import checkpoint
from viewer_relay import visible_terminal_text

TARGET = {
    "socket": "/tmp/native-test.sock", "workspace_id": "test-workspace",
    "workspace_label": "test workspace", "development_tab_id": "test-workspace:dev-tab",
    "development_pane_id": "test-workspace:dev-pane", "development_terminal_id": "test-terminal",
    "alacritty_executable": "/tmp/test-alacritty", "alacritty_pid": 1001,
    "alacritty_start": "synthetic start",
}

CALIBRATION = {
    "ax_bounds": {"X": 10, "Y": 20, "Width": 800, "Height": 600},
    "cg_window_id": 42,
    "png_scale": {"x": 2, "y": 2},
    "fixture_layout": {
        "area": {"x": 0, "y": 0, "width": 80, "height": 24},
        "pane": {"x": 0, "y": 0, "width": 80, "height": 24},
    },
    "viewport_grid": {"columns": 80, "rows": 24},
    "relative_crop_points": {"X": 175, "Y": 16, "Width": 400, "Height": 300},
}


def snapshot(*, fixture=None, focus=None, extra_fixture_pane=False):
    tabs = [
        {"tab_id": TARGET["development_tab_id"], "workspace_id": TARGET["workspace_id"], "label": "1"},
    ]
    panes = [{"pane_id": TARGET["development_pane_id"], "tab_id": TARGET["development_tab_id"],
              "terminal_id": TARGET["development_terminal_id"]}]
    if fixture:
        tabs.append({"tab_id": fixture["tab_id"], "workspace_id": TARGET["workspace_id"], "label": fixture["label"]})
        panes.append({"pane_id": fixture["pane_id"], "tab_id": fixture["tab_id"],
                      "terminal_id": fixture["terminal_id"]})
        if extra_fixture_pane:
            panes.append({"pane_id": "test-workspace:attacker", "tab_id": fixture["tab_id"], "terminal_id": "attacker"})
    focused_tab = focus if focus is not None else (fixture["tab_id"] if fixture else TARGET["development_tab_id"])
    layouts = []
    if fixture:
        layouts = [{"workspace_id": TARGET["workspace_id"], "tab_id": fixture["tab_id"],
                    "focused_pane_id": fixture["pane_id"], "area": dict(CALIBRATION["fixture_layout"]["area"]),
                    "panes": [{"pane_id": fixture["pane_id"], "rect": dict(CALIBRATION["fixture_layout"]["pane"])}]}]
    return {"workspaces": [{"workspace_id": TARGET["workspace_id"], "label": TARGET["workspace_label"]}], "tabs": tabs,
            "panes": panes, "focused_tab_id": focused_tab,
            "focused_pane_id": fixture["pane_id"] if focused_tab == (fixture or {}).get("tab_id") else TARGET["development_pane_id"],
            "layouts": layouts}


class FixtureRunner:
    def __init__(self):
        self.fixture = None
        self.commands = []
        self.extra_fixture_pane = False

    def __call__(self, args):
        self.commands.append(args)
        if args == ["api", "snapshot"]:
            return {"snapshot": snapshot(fixture=self.fixture,
                                          extra_fixture_pane=self.extra_fixture_pane)}
        if args[:2] == ["tab", "create"]:
            self.fixture = {"tab_id": "test-workspace:fixture", "pane_id": "test-workspace:fixture-pane",
                            "terminal_id": "term-fixture", "label": args[args.index("--label") + 1]}
            return {"tab": {"tab_id": self.fixture["tab_id"]},
                    "root_pane": {"pane_id": self.fixture["pane_id"], "terminal_id": self.fixture["terminal_id"]}}
        if args[:2] == ["tab", "close"]:
            self.fixture = None
            return {"closed": True}
        return {"ok": True}


class WorkspaceFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runner = FixtureRunner()
        self.owner = {"socket": {"device": 1, "inode": 2}, "owner": {"pid": 3, "start": "owner", "executable": "herdr"}}
        self.client = [{"pid": TARGET["alacritty_pid"], "start": TARGET["alacritty_start"]}]
        self.fixtures = []
        self.patches = [mock.patch("workspace_fixture.socket_owner", return_value=self.owner),
                        mock.patch("workspace_fixture.alacritty_census", return_value=self.client)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        for fixture in self.fixtures:
            if fixture.fixture_root is not None and fixture.fixture_root.exists():
                fixture.remove_runtime_root()
        self.temp.cleanup()

    def fixture(self):
        fixture = WorkspaceFixture(Path(self.temp.name) / ("evidence-" + str(len(self.fixtures))), TARGET, runner=self.runner)
        self.fixtures.append(fixture)
        return fixture

    def test_wrong_socket_workspace_pane_and_client_reject_before_create(self):
        fixture = self.fixture()
        with mock.patch("workspace_fixture.socket_owner", side_effect=OwnershipError("configured endpoint is not a socket")):
            with self.assertRaisesRegex(OwnershipError, "configured endpoint"):
                fixture.create()
        self.assertNotIn(["tab", "create"], [command[:2] for command in self.runner.commands])

        for name, altered, message in (("workspace", {"workspaces": []}, "workspace"),
                                       ("pane", {"panes": []}, "development tab/pane")):
            def runner(args, altered=altered):
                if args == ["api", "snapshot"]:
                    return {"snapshot": snapshot() | altered}
                raise AssertionError("mutation reached after " + name + " rejection")
            with self.assertRaisesRegex(OwnershipError, message):
                fixture = WorkspaceFixture(Path(self.temp.name) / ("evidence-" + name), TARGET, runner=runner)
                self.fixtures.append(fixture)
                fixture.create()
        with mock.patch("workspace_fixture.alacritty_census", return_value=[]):
            with self.assertRaisesRegex(OwnershipError, "Alacritty census"):
                fixture = WorkspaceFixture(Path(self.temp.name) / "evidence-client", TARGET, runner=self.runner)
                self.fixtures.append(fixture)
                fixture.create()

    def test_create_records_exact_ids_and_only_routes_to_recorded_pane(self):
        fixture = self.fixture()
        with mock.patch("workspace_fixture.subprocess.Popen") as popen:
            created = fixture.create()
            fixture.run("printf synthetic")
            popen.assert_not_called()
        self.assertEqual(created["tab_id"], "test-workspace:fixture")
        self.assertIn(["pane", "run", "test-workspace:fixture-pane", "printf synthetic"], self.runner.commands)
        self.assertEqual(fixture.fixture_root.parent, Path("/tmp").resolve())
        self.assertLess(len(os.fsencode(nvim_socket_path(fixture.fixture_root))), UNIX_SOCKET_PATH_MAX)
        runtime = json.loads((fixture.artifact / "runtime-root.json").read_text())
        self.assertEqual(runtime["socket"], str(nvim_socket_path(fixture.fixture_root)))
        fixture.close()
        self.assertFalse(fixture.fixture_root.exists())
        self.assertEqual(json.loads((fixture.artifact / "fixture-teardown.json").read_text())["status"], "PASS")

    def test_cleanup_refuses_an_unowned_pane_and_keeps_runtime_for_recovery(self):
        fixture = self.fixture()
        fixture.create()
        self.runner.extra_fixture_pane = True
        with self.assertRaisesRegex(OwnershipError, "unowned pane"):
            fixture.close()
        self.assertTrue(fixture.fixture_root.exists())
        self.assertNotIn(["tab", "close", "test-workspace:fixture"], self.runner.commands)
        receipt = json.loads((fixture.artifact / "fixture-teardown.json").read_text())
        self.assertEqual(receipt["status"], "FAIL")

    def test_retain_runtime_closes_owned_tab_but_preserves_exact_runtime_for_recovery(self):
        fixture = self.fixture()
        fixture.create()
        runtime = dict(fixture.runtime)
        fixture.retain_runtime("owned server identity changed")
        with self.assertRaisesRegex(OwnershipError, "runtime retained"):
            fixture.close()
        self.assertIsNone(fixture.fixture)
        self.assertTrue(fixture.fixture_root.exists())
        self.assertEqual(json.loads((fixture.artifact / "runtime-retained.json").read_text()), {
            "reason": "owned server identity changed", "runtime": runtime,
        })
        self.assertEqual(json.loads((fixture.artifact / "fixture-teardown.json").read_text())["status"], "FAIL")
        self.assertIn(["tab", "close", "test-workspace:fixture"], self.runner.commands)
        fixture.retention_reason = None
        fixture.remove_runtime_root()

    def test_primary_failure_is_preserved_when_cleanup_also_fails(self):
        fixture = self.fixture()
        fixture.create()
        fixture.primary_error = RuntimeError("save lane failed")
        self.runner.extra_fixture_pane = True
        with self.assertRaisesRegex(RuntimeError, "save lane failed") as raised:
            fixture.close()
        self.assertIsInstance(raised.exception.__cause__, OwnershipError)

    def test_process_identity_parses_the_ps_shape_used_by_live_guards(self):
        completed = mock.Mock(stdout="8763 Thu Sep 10 22:34:01 2026 /tmp/test-herdr\n")
        with mock.patch("workspace_fixture.subprocess.run", return_value=completed):
            self.assertEqual(process_identity(8763), {"pid": 8763, "start": "Thu Sep 10 22:34:01 2026",
                                                       "executable": "/tmp/test-herdr"})

    def test_silent_success_mutation_is_not_a_protocol_failure(self):
        fixture = self.fixture()
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("workspace_fixture.subprocess.run", return_value=completed):
            self.assertEqual(fixture._run(["pane", "run", "test-workspace:fixture-pane", "printf synthetic"]), {})

    def test_replacement_socket_owner_rejects_pane_run_before_the_mutation(self):
        fixture = self.fixture()
        fixture.create()
        replacement = {"socket": {"device": 9, "inode": 10},
                       "owner": {"pid": 4, "start": "replacement", "executable": "herdr"}}
        with mock.patch("workspace_fixture.socket_owner", return_value=replacement):
            with self.assertRaisesRegex(OwnershipError, "pre-create identity"):
                fixture.run("printf synthetic")
            with self.assertRaisesRegex(OwnershipError, "pre-create identity"):
                fixture.focus_for_input()
            with self.assertRaisesRegex(OwnershipError, "pre-create identity"):
                fixture.capture(viewport_grid=CALIBRATION["viewport_grid"])
        self.assertNotIn(["pane", "run", "test-workspace:fixture-pane", "printf synthetic"], self.runner.commands)
        self.assertNotIn(["tab", "focus", "test-workspace:fixture"], self.runner.commands)

    def test_extra_fixture_pane_rejects_run_input_and_capture_before_mutation(self):
        fixture = self.fixture()
        fixture.create()
        self.runner.extra_fixture_pane = True
        with self.assertRaisesRegex(OwnershipError, "unowned pane"):
            fixture.run("printf synthetic")
        with self.assertRaisesRegex(OwnershipError, "unowned pane"):
            fixture.focus_for_input()
        with self.assertRaisesRegex(OwnershipError, "unowned pane"):
            fixture.capture(viewport_grid=CALIBRATION["viewport_grid"])
        self.assertNotIn(["pane", "run", "test-workspace:fixture-pane", "printf synthetic"], self.runner.commands)
        self.assertNotIn(["tab", "focus", "test-workspace:fixture"], self.runner.commands)

    def test_capture_selects_one_existing_alacritty_window_by_pid_and_ax_bounds(self):
        bounds = {"X": 10, "Y": 20, "Width": 800, "Height": 600}
        row = {"pid": TARGET["alacritty_pid"], "window_id": 42, "layer": 0, "bounds": bounds}
        self.assertEqual(select_window({"windows": [row]}, TARGET["alacritty_pid"], bounds), row)
        with self.assertRaisesRegex(OwnershipError, "unique CoreGraphics"):
            select_window({"windows": [row, dict(row, window_id=43)]}, TARGET["alacritty_pid"], bounds)

    def test_calibrated_capture_plan_returns_the_fixture_rectangle_and_pixel_size(self):
        plan = calibrated_capture_plan(CALIBRATION, {"bounds": CALIBRATION["ax_bounds"]},
                                       {"window_id": 42}, CALIBRATION["fixture_layout"],
                                       CALIBRATION["viewport_grid"])
        self.assertEqual(plan["screen_rectangle_points"], {"X": 185, "Y": 36, "Width": 400, "Height": 300})
        self.assertEqual(plan["expected_pixels"], {"width": 800, "height": 600})

    def test_calibration_binds_observed_pty_grid_separately_from_pane_rectangle(self):
        calibration = copy.deepcopy(CALIBRATION)
        calibration["fixture_layout"]["pane"]["width"] = 191
        calibration["viewport_grid"]["columns"] = 190
        parsed = parse_capture_calibration(calibration)
        plan = calibrated_capture_plan(parsed, {"bounds": parsed["ax_bounds"]}, {"window_id": 42},
                                       parsed["fixture_layout"], {"columns": 190, "rows": 24})
        self.assertEqual(plan["bindings"], parsed)
        with self.assertRaisesRegex(OwnershipError, "parent viewport grid"):
            calibrated_capture_plan(parsed, {"bounds": parsed["ax_bounds"]}, {"window_id": 42},
                                    parsed["fixture_layout"], {"columns": 191, "rows": 24})
        calibration["viewport_grid"]["columns"] = 192
        with self.assertRaisesRegex(OwnershipError, "exceeds the fixture pane"):
            parse_capture_calibration(calibration)

    def test_calibrated_capture_rejects_absent_or_changed_bindings(self):
        ax = {"bounds": CALIBRATION["ax_bounds"]}
        cg = {"window_id": 42}
        with self.assertRaisesRegex(OwnershipError, "calibration is absent"):
            calibrated_capture_plan(None, ax, cg, CALIBRATION["fixture_layout"], CALIBRATION["viewport_grid"])
        cases = (("AX bounds", {"bounds": {**CALIBRATION["ax_bounds"], "Width": 801}}, cg,
                  CALIBRATION["fixture_layout"], CALIBRATION["viewport_grid"]),
                 ("CoreGraphics window", ax, {"window_id": 43}, CALIBRATION["fixture_layout"], CALIBRATION["viewport_grid"]),
                 ("fixture layout", ax, cg, {**CALIBRATION["fixture_layout"],
                                               "pane": {**CALIBRATION["fixture_layout"]["pane"], "width": 79}},
                  CALIBRATION["viewport_grid"]),
                 ("parent viewport grid", ax, cg, CALIBRATION["fixture_layout"], {"columns": 79, "rows": 24}))
        for expected, changed_ax, changed_cg, layout, grid in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(OwnershipError, expected):
                    calibrated_capture_plan(CALIBRATION, changed_ax, changed_cg, layout, grid)

    def test_fixture_layout_requires_one_recorded_pane(self):
        fixture = {"tab_id": "test-workspace:fixture", "pane_id": "test-workspace:fixture-pane"}
        observed = snapshot(fixture={**fixture, "terminal_id": "term", "label": "fixture"})
        self.assertEqual(fixture_layout(observed, fixture), CALIBRATION["fixture_layout"])
        observed["layouts"][0]["panes"].append({"pane_id": "attacker", "rect": CALIBRATION["fixture_layout"]["pane"]})
        with self.assertRaisesRegex(OwnershipError, "fixture layout changed"):
            fixture_layout(observed, fixture)

    def test_checkpoint_context_keeps_parent_and_display_connections_distinct(self):
        session = SimpleNamespace(session="isolated", socket=Path("/tmp/isolated.sock"), herdr="/tmp/herdr",
                                  env={"HOME": "/tmp/isolated-home", "PATH": "/usr/bin",
                                       "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data",
                                       "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache"})
        context = {"viewer_kind": "herdr", "native_target_file": "/tmp/private-target.json",
                   "isolated_session": "isolated", "isolated_socket": "/tmp/isolated.sock",
                   "workspace_id": "isolated-workspace", "tab_id": "isolated-workspace:tab",
                   "editor_pane": "isolated-workspace:pane", "render_marker": "PR00CAP-ABCDEF123456",
                   "retention": lambda: {"status": "PASS"}, "viewport_before": {"grid": [24, 80]},
                   "restore_viewport": lambda: {"status": "PASS"}}
        self.assertEqual(checkpoint_context(session, context), context)
        with self.assertRaisesRegex(OwnershipError, "not the owned isolated session"):
            checkpoint_context(session, context | {"isolated_socket": "/tmp/replacement.sock"})
        with self.assertRaisesRegex(OwnershipError, "viewer kind"):
            checkpoint_context(session, context | {"viewer_kind": "nvim"})
        with self.assertRaisesRegex(OwnershipError, "render marker"):
            checkpoint_context(session, context | {"render_marker": "1"})
        argv, executable, environment = viewer_argv(session, context)
        self.assertEqual(executable, "/tmp/herdr")
        self.assertEqual(argv[:2], ["/usr/bin/env", "-i"])
        self.assertIn("HERDR_SOCKET_PATH=/tmp/isolated.sock", argv)
        self.assertEqual(environment["HERDR_SOCKET_PATH"], "/tmp/isolated.sock")
        with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/tmp/parent.sock", "NVIM_APPNAME": "parent-config"}):
            probe = subprocess.run(argv[:2] + argv[2:-3] + ["/usr/bin/env"],
                                   capture_output=True, text=True, check=True)
        observed = dict(line.split("=", 1) for line in probe.stdout.splitlines())
        self.assertEqual(observed, environment)
        self.assertNotIn("NVIM_APPNAME", observed)
        with mock.patch("checkpoint.os.kill") as signal_viewer:
            with self.assertRaisesRegex(OwnershipError, "resource retained"):
                stop_viewer(123, None)
            signal_viewer.assert_not_called()
        with mock.patch("checkpoint.process_identity", side_effect=subprocess.CalledProcessError(1, "ps")), \
             mock.patch("checkpoint.os.kill") as signal_viewer:
            stop_viewer(123, {"pid": 123, "start": "verified", "executable": "/tmp/viewer"})
            signal_viewer.assert_not_called()

    def test_stop_viewer_allows_exec_transition_but_rejects_reused_pid(self):
        verified = {"pid": 123, "start": "verified"}
        post_exec = {"pid": 123, "start": "verified", "executable": "/tmp/real-viewer"}
        with mock.patch("checkpoint.process_identity", side_effect=[post_exec, subprocess.CalledProcessError(1, "ps")]), \
             mock.patch("checkpoint.os.kill") as signal_viewer:
            stop_viewer(123, verified)
            signal_viewer.assert_called_once()
        replacement = {"pid": 123, "start": "replacement", "executable": "/tmp/real-viewer"}
        with mock.patch("checkpoint.process_identity", return_value=replacement), mock.patch("checkpoint.os.kill") as signal_viewer:
            with self.assertRaisesRegex(OwnershipError, "identity changed"):
                stop_viewer(123, verified)
            signal_viewer.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "UNVERIFIED: proc_pidpath is a macOS-only kernel probe")
    def test_kernel_image_path_observes_a_real_harmless_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            identity = process_identity(child.pid)
            observed = checkpoint.execution_observation(identity)
            self.assertTrue(Path(observed["kernel_image_path"]).is_absolute())
            self.assertEqual(observed["kernel_image_path"], str(Path(sys.executable).resolve()))
        finally:
            child.terminate(); child.wait(timeout=2)

    def test_bare_ps_command_is_diagnostic_not_kernel_authority(self):
        identity = {"pid": 123, "start": "stable", "executable": "nvim", "kernel_image_path": "/tmp/nvim"}
        with mock.patch("checkpoint.process_identity", return_value=identity):
            observed = checkpoint.execution_observation(identity)
        self.assertEqual(observed["raw_command"], "nvim")
        self.assertEqual(observed["kernel_image_path"], "/tmp/nvim")

    def test_kernel_image_failure_keeps_raw_observation(self):
        identity = {"pid": 123, "start": "stable", "executable": "bare-name"}
        with mock.patch("checkpoint.kernel_image_path", side_effect=OSError("denied")), \
             mock.patch("checkpoint.process_identity", return_value=identity):
            observed = checkpoint.execution_observation(identity)
        self.assertEqual(observed["raw_command"], "bare-name")
        self.assertIn("denied", observed["kernel_image_error"])


    def test_viewer_relay_records_post_exec_marker_and_pty_grid(self):
        root = Path(self.temp.name)
        fake = root / "fake-renderer.sh"
        receipt = root / "relay.json"
        output = root / "render.bin"
        fake.write_text('#!/bin/sh\nstty raw -echo\nprintf "\\033[6n"\ndd bs=1 count=6 of=/dev/null 2>/dev/null\nprintf "PR00CAP-ABCDEF123456\\n"\nsleep 5\n')
        fake.chmod(0o700)
        relay = Path(__file__).with_name("viewer_relay.py")
        process = subprocess.Popen([sys.executable, str(relay), "--receipt", str(receipt), "--output", str(output),
                                    "--marker", "PR00CAP-ABCDEF123456", "--rows", "24", "--columns", "80", "--", str(fake)],
                                   stdout=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 2
            while (not receipt.exists() or not json.loads(receipt.read_text()).get("rendered")) and time.monotonic() < deadline:
                time.sleep(.01)
            rendered = json.loads(receipt.read_text())
            self.assertTrue(rendered["rendered"])
            self.assertEqual(rendered["grid"], {"rows": 24, "columns": 80})
        finally:
            process.terminate()
            process.wait(timeout=2)
        self.assertIn(b"PR00CAP-ABCDEF123456", output.read_bytes())

    def test_viewer_marker_requires_visible_complete_text(self):
        marker = "PR00CAP-ABCDEF123456"
        nested_error = b"error: nested herdr is disabled\x1b[1m\n"
        self.assertNotIn("1", visible_terminal_text(nested_error))
        self.assertIn(marker, visible_terminal_text(b"viewer ready " + marker.encode() + b"\n"))
        self.assertIn(marker, visible_terminal_text(("❯ " + marker).encode()))
        self.assertIn(marker, visible_terminal_text(("➛ " + marker).encode()))
        self.assertNotIn(marker, visible_terminal_text(b"\x1b]title:" + marker.encode() + b"\x07"))
        self.assertNotIn(marker, visible_terminal_text(b"\x9dtitle:" + marker.encode() + b"\x9c"))
        self.assertNotIn(marker, visible_terminal_text(b"\x1b[31;" + marker.encode()))

    def test_checkpoint_invalid_target_keeps_primary_error_receipt(self):
        root = Path(self.temp.name) / "checkpoint"
        snapshot = {"tabs": [], "panes": [], "focused_tab_id": "prior", "focused_pane_id": "prior-pane",
                    "focused_workspace_id": "prior-workspace", "layouts": []}
        class Session:
            session = "isolated"
            socket = Path("/tmp/isolated.sock")
            herdr = "/tmp/herdr"
            env = {"HOME": "/tmp/home", "PATH": "/usr/bin", "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data", "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache"}
            def run(self, *args):
                return SimpleNamespace(stdout=json.dumps({"result": {"snapshot": snapshot}}))
        class Fixture:
            def __init__(self, artifact, target):
                self.artifact = Path(artifact); self.fixture_root = self.artifact / "runtime"; self.fixture = {"pane_id": "parent"}
            def __enter__(self): self.fixture_root.mkdir(parents=True); return self
            def __exit__(self, *unused): return False
        context = {"viewer_kind": "herdr", "native_target_file": "/tmp/private-target.json", "isolated_session": "isolated",
                   "isolated_socket": "/tmp/isolated.sock", "workspace_id": "missing", "tab_id": "missing-tab",
                   "editor_pane": "missing-pane", "render_marker": "PR00CAP-ABCDEF123456", "retention": lambda: {"status": "PASS"},
                   "viewport_before": {"grid": [24, 80]}, "restore_viewport": lambda: {"status": "PASS"}}
        with mock.patch("checkpoint.load_target", return_value=TARGET), mock.patch("checkpoint.WorkspaceFixture", Fixture):
            with self.assertRaisesRegex(OwnershipError, "requested display target"):
                capture_checkpoint(Session(), root.parent, root.name, context)
        receipt = json.loads((root / "checkpoint.json").read_text())
        self.assertEqual(receipt["status"], "FAIL")
        self.assertIn("requested display target", receipt["primary_error"])

    def test_checkpoint_context_receipt_precedes_fixture_entry_and_focus(self):
        root = Path(self.temp.name) / "entry-failure"
        calls = []
        class Session:
            session = "isolated"; socket = Path("/tmp/isolated.sock"); herdr = "/tmp/herdr"; env = {"HOME": "/tmp/home", "PATH": "/usr/bin", "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data", "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache"}
            def run(self, *args): calls.append(args); raise AssertionError("focus must not run")
        class Fixture:
            def __init__(self, *unused): pass
            def __enter__(self): raise OwnershipError("fixture entry failed")
            def __exit__(self, *unused): return False
        context = {"viewer_kind":"herdr","native_target_file":"/tmp/private-target.json","isolated_session":"isolated","isolated_socket":"/tmp/isolated.sock","workspace_id":"workspace","tab_id":"tab","editor_pane":"editor","render_marker":"PR00CAP-ABCDEF123456","retention":lambda:{"retained":"PASS"},"viewport_before":{"grid":[24,80]},"restore_viewport":lambda:{"status":"PASS"}}
        with mock.patch("checkpoint.load_target", return_value=TARGET), mock.patch("checkpoint.WorkspaceFixture", Fixture):
            with self.assertRaisesRegex(OwnershipError, "fixture entry failed"):
                capture_checkpoint(Session(), root.parent, root.name, context)
        saved = json.loads((root.parent / (root.name + ".context.json")).read_text())
        self.assertEqual(saved["requested"], {key: context[key] for key in ("viewer_kind", "isolated_session", "isolated_socket",
                                                                               "workspace_id", "tab_id", "editor_pane", "render_marker",
                                                                               "viewport_before", "native_target_file")})
        self.assertEqual(saved["expected_viewer_executable"], {"raw_executable": "/tmp/herdr",
                                                                  "canonical_executable": str(Path("/tmp/herdr").resolve())})
        self.assertEqual(saved["viewer_environment"], {"HOME": "/tmp/home", "PATH": "/usr/bin",
                                                         "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data",
                                                         "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache",
                                                         "HERDR_SOCKET_PATH": "/tmp/isolated.sock", "TERM": "xterm-256color"})
        self.assertEqual(calls, [])
        self.assertFalse(root.exists())

    def test_checkpoint_success_records_relay_render_and_parent_grid(self):
        root = Path(self.temp.name) / "success"
        state = {"tabs": [{"tab_id": "tab", "workspace_id": "workspace", "label": "original-label"}],
                 "panes": [{"pane_id": "editor", "tab_id": "tab"}], "focused_tab_id": "tab",
                 "focused_pane_id": "other", "focused_workspace_id": "workspace", "layouts": []}
        class Session:
            session = "isolated"; socket = Path("/tmp/isolated.sock"); herdr = "/tmp/herdr"; env = {"HOME": "/tmp/home", "PATH": "/usr/bin", "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data", "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache"}
            def run(self, *args):
                if args[:2] == ("tab", "rename"):
                    state["tabs"][0]["label"] = args[3]
                if args[:2] == ("tab", "focus"):
                    state["focused_tab_id"] = args[2]
                return SimpleNamespace(stdout=json.dumps({"result": {"snapshot": state}}))
        class Fixture:
            def __init__(self, artifact, target): self.artifact=Path(artifact); self.fixture_root=self.artifact/"runtime"; self.fixture={"pane_id":"parent"}
            def __enter__(self): self.fixture_root.mkdir(parents=True); return self
            def __exit__(self, *unused): return False
            def run(self, command): (self.fixture_root / "viewer-relay.json").write_text(json.dumps({"rendered":True,"child_pid":99,"relay_pid":98,"child_identity":{"pid":99,"start":"start","raw_command":"bare-viewer","kernel_image_path":str(Path("/tmp/herdr").resolve())},"relay_identity":{"pid":98,"start":"start","raw_command":"python3","kernel_image_path":str(Path(sys.executable).resolve())}}))
            def parent_pty_grid(self): return {"rows":24,"columns":80,"tty":"/dev/ttys001","shell_pid":5}
            def capture(self, **kwargs): return {"status":"PNG","grid":kwargs["viewport_grid"]}
        context = {"viewer_kind":"herdr","native_target_file":"/tmp/private-target.json","isolated_session":"isolated","isolated_socket":"/tmp/isolated.sock","workspace_id":"workspace","tab_id":"tab","editor_pane":"editor","render_marker":"PR00CAP-ABCDEF123456","retention":lambda:{"retained":"PASS"},"viewport_before":{"grid":[24,80]},"restore_viewport":lambda:{"status":"PASS"}}
        preflight = SimpleNamespace(stdout=json.dumps({"result":{"snapshot":state}}))
        identity = {"pid":99,"executable":"/tmp/herdr","start":"start","kernel_image_path":str(Path("/tmp/herdr").resolve())}
        with mock.patch("checkpoint.load_target", return_value=TARGET), mock.patch("checkpoint.WorkspaceFixture", Fixture), \
             mock.patch("checkpoint.subprocess.run", return_value=preflight), mock.patch("checkpoint.process_identity", return_value=identity), \
             mock.patch("checkpoint.stop_viewer") as stopped:
            result = capture_checkpoint(Session(), root.parent, root.name, context)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["parent_viewport"]["columns"], 80)
        self.assertTrue(result["render"]["rendered"])
        self.assertEqual(result["render_marker_restore"], "PASS")
        self.assertEqual(state["tabs"][0]["label"], "original-label")
        self.assertEqual(stopped.call_count, 2)
        stopped.assert_any_call(99, {"pid": 99, "start": "start"})
        early = json.loads((root / "viewer-early.json").read_text())
        self.assertEqual(early["viewer_execution"]["early"]["raw_command"], "bare-viewer")

    def test_checkpoint_accepts_env_exec_to_the_requested_symlink(self):
        root = Path(self.temp.name) / "exec-transition"
        viewer = Path(self.temp.name) / "viewer-link"
        viewer.symlink_to(Path(sys.executable).resolve())
        state = {"tabs": [{"tab_id": "tab", "workspace_id": "workspace", "label": "original-label"}],
                 "panes": [{"pane_id": "editor", "tab_id": "tab"}], "focused_tab_id": "tab",
                 "focused_pane_id": "other", "focused_workspace_id": "workspace", "layouts": []}
        class Session:
            session = "isolated"; socket = Path("/tmp/isolated.sock"); herdr = str(viewer); env = {"HOME": "/tmp/home", "PATH": "/usr/bin", "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data", "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache"}
            def run(self, *args):
                if args[:2] == ("tab", "rename"):
                    state["tabs"][0]["label"] = args[3]
                return SimpleNamespace(stdout=json.dumps({"result": {"snapshot": state}}))
        class Fixture:
            def __init__(self, artifact, target): self.artifact=Path(artifact); self.fixture_root=self.artifact/"runtime"; self.fixture={"pane_id":"parent"}
            def __enter__(self): self.fixture_root.mkdir(parents=True); return self
            def __exit__(self, *unused): return False
            def run(self, command):
                (self.fixture_root / "viewer-relay.json").write_text(json.dumps({
                    "rendered": True, "child_pid": 99, "relay_pid": 98,
                    "child_identity": {"pid": 99, "start": "start", "raw_executable": "/usr/bin/env",
                                       "canonical_executable": "/usr/bin/env"},
                    "relay_identity": {"pid": 98, "start": "relay", "raw_executable": "/usr/bin/python3",
                                       "canonical_executable": "/usr/bin/python3"}}))
            def parent_pty_grid(self): return {"rows":24,"columns":80,"tty":"/dev/ttys001","shell_pid":5}
            def capture(self, **kwargs): return {"status": "PNG"}
        context = {"viewer_kind":"herdr","native_target_file":"/tmp/private-target.json","isolated_session":"isolated","isolated_socket":"/tmp/isolated.sock","workspace_id":"workspace","tab_id":"tab","editor_pane":"editor","render_marker":"PR00CAP-ABCDEF123456","retention":lambda:{"retained":"PASS"},"viewport_before":{"grid":[24,80]},"restore_viewport":lambda:{"status":"PASS"}}
        preflight = SimpleNamespace(stdout=json.dumps({"result": {"snapshot": state}}))
        post_exec = {"pid": 99, "start": "start", "executable": str(viewer), "kernel_image_path": str(Path(sys.executable).resolve())}
        def argv(session, context): return ["/usr/bin/env", str(viewer)], str(viewer), {"HERDR_SOCKET_PATH": str(session.socket)}
        with mock.patch("checkpoint.load_target", return_value=TARGET), mock.patch("checkpoint.WorkspaceFixture", Fixture), \
             mock.patch("checkpoint.viewer_argv", side_effect=argv), mock.patch("checkpoint.subprocess.run", return_value=preflight), \
             mock.patch("checkpoint.process_identity", return_value=post_exec), mock.patch("checkpoint.stop_viewer") as stopped:
            receipt = capture_checkpoint(Session(), root.parent, root.name, context)
        execution = receipt["viewer_execution"]
        self.assertEqual(execution["early"]["raw_command"], "/usr/bin/env")
        self.assertEqual(execution["post_render"]["raw_command"], str(viewer))
        self.assertEqual(execution["post_render"]["kernel_image_path"], str(Path(sys.executable).resolve()))
        stopped.assert_any_call(99, {"pid": 99, "start": "start"})

    def test_checkpoint_persists_post_render_mismatch_before_capture(self):
        root = Path(self.temp.name) / "execution-mismatch"
        state = {"tabs": [{"tab_id": "tab", "workspace_id": "workspace", "label": "original-label"}],
                 "panes": [{"pane_id": "editor", "tab_id": "tab"}], "focused_tab_id": "tab",
                 "focused_pane_id": "other", "focused_workspace_id": "workspace", "layouts": []}
        class Session:
            session = "isolated"; socket = Path("/tmp/isolated.sock"); herdr = "/tmp/expected-viewer"; env = {"HOME": "/tmp/home", "PATH": "/usr/bin", "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data", "XDG_STATE_HOME": "/tmp/state", "XDG_CACHE_HOME": "/tmp/cache"}
            def run(self, *args):
                if args[:2] == ("tab", "rename"):
                    state["tabs"][0]["label"] = args[3]
                return SimpleNamespace(stdout=json.dumps({"result": {"snapshot": state}}))
        class Fixture:
            instances = []
            def __init__(self, artifact, target):
                self.artifact=Path(artifact); self.fixture_root=self.artifact/"runtime"; self.fixture={"pane_id":"parent"}; self.capture_calls=0
                Fixture.instances.append(self)
            def __enter__(self): self.fixture_root.mkdir(parents=True); return self
            def __exit__(self, *unused): return False
            def run(self, command):
                (self.fixture_root / "viewer-relay.json").write_text(json.dumps({
                    "rendered": True, "child_pid": 99, "relay_pid": 98,
                    "child_identity": {"pid": 99, "start": "start", "raw_executable": "/usr/bin/env",
                                       "canonical_executable": "/usr/bin/env"},
                    "relay_identity": {"pid": 98, "start": "relay", "raw_executable": "/usr/bin/python3",
                                       "canonical_executable": "/usr/bin/python3"}}))
            def parent_pty_grid(self): return {"rows":24,"columns":80,"tty":"/dev/ttys001","shell_pid":5}
            def capture(self, **kwargs): self.capture_calls += 1
        context = {"viewer_kind":"herdr","native_target_file":"/tmp/private-target.json","isolated_session":"isolated","isolated_socket":"/tmp/isolated.sock","workspace_id":"workspace","tab_id":"tab","editor_pane":"editor","render_marker":"PR00CAP-ABCDEF123456","retention":lambda:{"retained":"PASS"},"viewport_before":{"grid":[24,80]},"restore_viewport":lambda:{"status":"PASS"}}
        preflight = SimpleNamespace(stdout=json.dumps({"result": {"snapshot": state}}))
        replacement = {"pid": 99, "start": "start", "executable": "/tmp/replaced-viewer", "kernel_image_path": "/tmp/replaced-viewer"}
        with mock.patch("checkpoint.load_target", return_value=TARGET), mock.patch("checkpoint.WorkspaceFixture", Fixture), \
             mock.patch("checkpoint.subprocess.run", return_value=preflight), mock.patch("checkpoint.process_identity", return_value=replacement), \
             mock.patch("checkpoint.stop_viewer"):
            with self.assertRaisesRegex(OwnershipError, "executable changed"):
                capture_checkpoint(Session(), root.parent, root.name, context)
        post = json.loads((root / "viewer-post-render.json").read_text())
        self.assertEqual(post["expected"]["raw_executable"], "/tmp/expected-viewer")
        self.assertEqual(post["post_render"]["raw_command"], "/tmp/replaced-viewer")
        self.assertEqual(Fixture.instances[-1].capture_calls, 0)

    def test_checkpoint_render_timeout_stops_owned_pair_then_restores(self):
        root = Path(self.temp.name) / "timeout"
        state = {"tabs":[{"tab_id":"tab","workspace_id":"workspace","label":"original-label"}],"panes":[{"pane_id":"editor","tab_id":"tab"}],"focused_tab_id":"tab","focused_pane_id":"other","focused_workspace_id":"workspace","layouts":[]}
        order = []
        class Session:
            session="isolated"; socket=Path("/tmp/isolated.sock"); herdr="/tmp/herdr"; env={"HOME":"/tmp/home","PATH":"/usr/bin","XDG_CONFIG_HOME":"/tmp/config","XDG_DATA_HOME":"/tmp/data","XDG_STATE_HOME":"/tmp/state","XDG_CACHE_HOME":"/tmp/cache"}
            def run(self,*args):
                if args[:2] == ("tab", "rename"):
                    state["tabs"][0]["label"] = args[3]
                return SimpleNamespace(stdout=json.dumps({"result":{"snapshot":state}}))
        class Fixture:
            def __init__(self,a,t): self.artifact=Path(a); self.fixture_root=self.artifact/"runtime"; self.fixture={"pane_id":"parent"}
            def __enter__(self): self.fixture_root.mkdir(parents=True); return self
            def __exit__(self,*x): return False
            def run(self,x): (self.fixture_root/"viewer-relay.json").write_text(json.dumps({"rendered":False,"child_pid":99,"relay_pid":98,"child_identity":{"pid":99,"start":"start","raw_executable":"/tmp/herdr","canonical_executable":"/tmp/herdr"},"relay_identity":{"pid":98,"start":"start","raw_executable":"/usr/bin/python3","canonical_executable":"/usr/bin/python3"}}))
            def parent_pty_grid(self): return {"rows":24,"columns":80,"tty":"/dev/ttys001","shell_pid":5}
        context={"viewer_kind":"herdr","native_target_file":"/tmp/private-target.json","isolated_session":"isolated","isolated_socket":"/tmp/isolated.sock","workspace_id":"workspace","tab_id":"tab","editor_pane":"editor","render_marker":"PR00CAP-ABCDEF123456","retention":lambda:{"retained":"PASS"},"viewport_before":{"grid":[24,80]},"restore_viewport":lambda:order.append("restore") or {"status":"PASS"}}
        identity={"pid":99,"executable":"/tmp/herdr","start":"start"}
        calls=[]
        def wait(value,label,timeout=5):
            if label == "native checkpoint viewer did not render target text": raise OwnershipError(label)
            return value()
        with mock.patch("checkpoint.load_target",return_value=TARGET),mock.patch("checkpoint.WorkspaceFixture",Fixture),mock.patch("checkpoint.subprocess.run",return_value=SimpleNamespace(stdout=json.dumps({"result":{"snapshot":state}}))),mock.patch("checkpoint.process_identity",return_value=identity),mock.patch("checkpoint.wait_for",side_effect=wait),mock.patch("checkpoint.stop_viewer",side_effect=lambda pid,ident:calls.append(pid)):
            with self.assertRaisesRegex(OwnershipError,"did not render"):
                capture_checkpoint(Session(),root.parent,root.name,context)
        self.assertEqual(calls,[99,98]); self.assertEqual(order,["restore"])
        receipt=json.loads((root/"checkpoint.json").read_text())
        self.assertEqual(receipt["status"],"FAIL"); self.assertIn("did not render",receipt["primary_error"])
        self.assertEqual(receipt["render_marker_restore"], "PASS")
        self.assertEqual(state["tabs"][0]["label"], "original-label")

    def test_nvim_socket_path_rejects_a_sockaddr_un_overflow(self):
        long_root = Path("/tmp") / ("x" * UNIX_SOCKET_PATH_MAX)
        with self.assertRaisesRegex(OwnershipError, "sockaddr_un"):
            nvim_socket_path(long_root)

    def test_key_log_reads_non_utf8_input_as_ascii_hex(self):
        path = Path(self.temp.name) / "keys.jsonl"
        path.write_bytes(b'{"key_hex":"fd","typed_hex":"fd"}\n')
        self.assertEqual(keylog_records(path), [{"key_hex": "fd", "typed_hex": "fd"}])
        path.write_bytes(b"")
        with self.assertRaisesRegex(RuntimeError, "empty"):
            keylog_records(path)

    def test_required_lane_or_recorded_failure_exits_nonzero(self):
        passed = {"cmd-enter": "PASS", "ctrl-s": "PASS", "screenshot": "PASS", "failures": []}
        self.assertEqual(result_exit_code(passed), 0)
        self.assertEqual(result_exit_code(passed | {"screenshot": "UNVERIFIED"}), 2)
        self.assertEqual(result_exit_code(passed | {"failures": [{"operation": "keylog"}]}), 2)

    def test_target_file_requires_complete_mode_0600_contract(self):
        path = Path(self.temp.name) / "target.json"
        with self.assertRaisesRegex(OwnershipError, "unavailable"):
            load_target(path)
        path.write_text(json.dumps(TARGET))
        path.chmod(0o600)
        self.assertEqual(load_target(path), TARGET)
        path.write_text("{}")
        with self.assertRaisesRegex(OwnershipError, "target contract"):
            load_target(path)
        path.write_text(json.dumps(TARGET | {"capture_calibration": CALIBRATION}))
        path.chmod(0o600)
        loaded = load_target(path)
        self.assertEqual(loaded["capture_calibration"], CALIBRATION)
        malformed = copy.deepcopy(CALIBRATION)
        malformed["relative_crop_points"]["Width"] = 400.25
        path.write_text(json.dumps(TARGET | {"capture_calibration": malformed}))
        with self.assertRaisesRegex(OwnershipError, "whole PNG pixels"):
            load_target(path)


if __name__ == "__main__":
    unittest.main()
