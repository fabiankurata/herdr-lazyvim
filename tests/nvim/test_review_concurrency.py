#!/usr/bin/env python3
"""Cross-process review-store regressions against the actual Neovim module."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tests/live"))
from runtime_lease import require_runtime_lease

require_runtime_lease(repo=REPO)

IDENTITY = "00000000-0000-4000-8000-000000000001"


class ReviewConcurrencyTests(unittest.TestCase):
    def environment(self, root, process, shared_state, control, role, scenario=None):
        process_root = root / process
        values = {
            key: value for key, value in os.environ.items()
            if key.startswith("HERDR_RUNTIME_LEASE_") or key in {"PATH", "LC_ALL", "LANG"}
        }
        for key, name in {
            "HOME": "home",
            "XDG_CONFIG_HOME": "config",
            "XDG_CACHE_HOME": "cache",
            "XDG_DATA_HOME": "data",
            "XDG_RUNTIME_DIR": "runtime",
        }.items():
            path = process_root / name
            path.mkdir(parents=True)
            values[key] = str(path)
        values.update({
            "XDG_STATE_HOME": str(shared_state),
            "HERDR_TEST_REPO": str(REPO),
            "HERDR_REVIEW_ROOT": str(root / "worktree"),
            "HERDR_REVIEW_CONTROL": str(control),
            "HERDR_REVIEW_ROLE": role,
            "HERDR_NVIM_TEST_SCRIPT": str(HERE / "review_concurrency_worker.lua"),
        })
        if scenario:
            values["HERDR_REVIEW_SCENARIO"] = scenario
        return values

    def lock_environment(self, root, process, shared_state, control, role, scenario):
        values = self.environment(root, process, shared_state, control, role, scenario)
        values["HERDR_NVIM_TEST_SCRIPT"] = str(HERE / "review_lock_worker.lua")
        return values

    def fixture(self, root):
        worktree = root / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        (worktree / "example.lua").write_text("source\n")
        canonical = worktree.resolve()
        shared_state = root / "shared-state"
        state_dir = shared_state / "nvim/herdr-review"
        state_dir.mkdir(parents=True)
        state_path = state_dir / (hashlib.sha256(str(canonical).encode()).hexdigest()[:16] + ".json")
        control = root / "control"
        control.mkdir()
        return canonical, shared_state, state_path, control

    def start_lock_worker(self, root, name, shared_state, control, role, scenario):
        return subprocess.Popen([
            "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l", str(HERE / "run_test.lua"),
        ], cwd=REPO, env=self.lock_environment(root, name, shared_state, control, role, scenario),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def finish_worker(self, process, label, expected=0):
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, expected, f"{label}:\nstdout:\n{stdout}\nstderr:\n{stderr}")

    def stop_worker(self, process):
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
        self.assertIsNotNone(process.returncode)

    def wait_for_either(self, paths, process):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            for path in paths:
                if path.exists():
                    return path
            time.sleep(0.02)
        stdout, stderr = process.communicate(timeout=3)
        self.fail(f"worker did not reach a lock barrier:\nstdout:\n{stdout}\nstderr:\n{stderr}")

    def wait_for(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        if path.exists():
            return
        if process.poll() is None:
            process.terminate()
        stdout, stderr = process.communicate(timeout=3)
        self.fail(f"worker did not create {path.name}:\nstdout:\n{stdout}\nstderr:\n{stderr}")

    def test_send_preparation_never_flushes_another_editor_newer_state(self):
        for scenario in ("empty-selection", "picker-cancel", "discovery-failure"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix="herdr-review-", dir="/tmp") as temporary:
                root = Path(temporary)
                worktree = root / "worktree"
                worktree.mkdir()
                (worktree / ".git").mkdir()
                (worktree / "example.lua").write_text("source\n")
                canonical = worktree.resolve()
                shared_state = root / "shared-state"
                state_dir = shared_state / "nvim/herdr-review"
                state_dir.mkdir(parents=True)
                state_path = state_dir / (hashlib.sha256(str(canonical).encode()).hexdigest()[:16] + ".json")
                state_path.write_text(json.dumps({
                    "version": 1,
                    "root": str(canonical),
                    "comments": [{
                        "id": IDENTITY,
                        "revision": 1,
                        "file": "example.lua",
                        "start": 1,
                        "finish": 1,
                        "lines": "source",
                        "text": "revision one",
                    }],
                }) + "\n")
                control = root / "control"
                control.mkdir()
                reader = subprocess.Popen([
                    "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l", str(HERE / "run_test.lua"),
                ], cwd=REPO, env=self.environment(root, "reader", shared_state, control, "reader", scenario),
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.wait_for(control / "reader-ready", reader)
                    writer = subprocess.run([
                        "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l", str(HERE / "run_test.lua"),
                    ], cwd=REPO, env=self.environment(root, "writer", shared_state, control, "writer"),
                        text=True, capture_output=True, timeout=10)
                    self.assertEqual(writer.returncode, 0, writer.stdout + writer.stderr)
                    (control / "continue").write_text("continue\n")
                    stdout, stderr = reader.communicate(timeout=10)
                    self.assertEqual(reader.returncode, 0, stdout + stderr)
                finally:
                    if reader.poll() is None:
                        reader.terminate()
                    reader.communicate(timeout=3)
                persisted = json.loads(state_path.read_text())
                self.assertEqual(len(persisted["comments"]), 2, persisted)
                self.assertEqual(
                    [(item["id"], item["revision"], item["text"]) for item in persisted["comments"]],
                    [
                        (IDENTITY, 2, "revision two"),
                        (persisted["comments"][1]["id"], 1, "new annotation"),
                    ],
                )

    def test_headless_runner_exits_nonzero_on_script_error(self):
        with tempfile.TemporaryDirectory(prefix="herdr-nvim-failure-", dir="/tmp") as temporary:
            root = Path(temporary)
            script = root / "failure.lua"
            script.write_text("error('intentional headless fixture failure')\n")
            environment = self.environment(root, "failure", root / "state", root, "failure")
            environment["HERDR_NVIM_TEST_SCRIPT"] = str(script)
            result = subprocess.run([
                "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l", str(HERE / "run_test.lua"),
            ], cwd=REPO, env=environment, text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("intentional headless fixture failure", result.stderr)

    def test_competing_updates_retain_both_results_across_stale_reclaim_schedule(self):
        with tempfile.TemporaryDirectory(prefix="herdr-review-lock-", dir="/tmp") as temporary:
            root = Path(temporary)
            canonical, shared_state, state_path, control = self.fixture(root)
            dead = subprocess.Popen(["/usr/bin/true"])
            dead.wait(timeout=3)
            legacy = Path(str(state_path) + ".lock")
            legacy.mkdir()
            (legacy / "owner.json").write_text(json.dumps({"pid": dead.pid, "token": "dead-owner"}) + "\n")
            first = self.start_lock_worker(root, "first", shared_state, control, "first", "stale-reclaim")
            second = None
            try:
                barrier = self.wait_for_either(
                    [control / "first-legacy-reclaim", control / "first-kernel-snapshot"], first)
                second = self.start_lock_worker(root, "second", shared_state, control, "second", "stale-reclaim")
                self.wait_for(control / "second-started", second)
                if barrier.name == "first-legacy-reclaim":
                    self.wait_for(control / "second-legacy-snapshot", second)
                else:
                    (control / "release-first").write_text("release\n")
                self.finish_worker(first, "first participating update")
                self.finish_worker(second, "second participating update")
            finally:
                for process in (first, second):
                    if process is not None and process.poll() is None:
                        self.stop_worker(process)
            persisted = json.loads(state_path.read_text())
            self.assertEqual(sorted(item["text"] for item in persisted["comments"]),
                             ["first update", "second update"])

    def test_live_owner_contention_refuses_without_losing_state_or_draft(self):
        with tempfile.TemporaryDirectory(prefix="herdr-review-lock-", dir="/tmp") as temporary:
            root = Path(temporary)
            canonical, shared_state, state_path, control = self.fixture(root)
            state_path.write_text(json.dumps({
                "version": 1,
                "root": str(canonical),
                "comments": [{
                    "id": IDENTITY,
                    "revision": 1,
                    "file": "example.lua",
                    "start": 1,
                    "finish": 1,
                    "lines": "source",
                    "text": "committed base",
                }],
            }) + "\n")
            original = state_path.read_bytes()
            owner = self.start_lock_worker(root, "owner", shared_state, control, "owner", "live-refusal")
            contender = None
            try:
                self.wait_for(control / "owner-snapshot", owner)
                contender = self.start_lock_worker(root, "contender", shared_state, control, "contender", "live-refusal")
                self.finish_worker(contender, "bounded contender refusal")
                self.assertEqual(state_path.read_bytes(), original, "refused writer changed committed state")
                draft = json.loads((control / "contender-draft.json").read_text())
                self.assertEqual(draft, {"lines": ["contender draft"], "retained": True})
                (control / "release-owner").write_text("release\n")
                self.finish_worker(owner, "live owner")
            finally:
                for process in (owner, contender):
                    if process is not None and process.poll() is None:
                        self.stop_worker(process)
            persisted = json.loads(state_path.read_text())
            self.assertEqual([item["text"] for item in persisted["comments"]],
                             ["committed base", "owner update"])

    def test_process_death_during_acquisition_is_recoverable(self):
        with tempfile.TemporaryDirectory(prefix="herdr-review-lock-", dir="/tmp") as temporary:
            root = Path(temporary)
            _, shared_state, state_path, control = self.fixture(root)
            abandoned = self.start_lock_worker(root, "abandoned", shared_state, control, "abandoned", "abandon-acquire")
            barrier = self.wait_for_either(
                [control / "legacy-owner-unpublished", control / "kernel-acquired"], abandoned)
            self.stop_worker(abandoned)
            self.assertTrue(barrier.exists())
            recovery = self.start_lock_worker(root, "recovery", shared_state, control, "recovery", "recover")
            self.finish_worker(recovery, "acquisition recovery")
            persisted = json.loads(state_path.read_text())
            self.assertEqual([item["text"] for item in persisted["comments"]], ["recovery update"])

    def test_process_death_during_release_is_recoverable(self):
        with tempfile.TemporaryDirectory(prefix="herdr-review-lock-", dir="/tmp") as temporary:
            root = Path(temporary)
            _, shared_state, state_path, control = self.fixture(root)
            abandoned = self.start_lock_worker(root, "abandoned", shared_state, control, "abandoned", "abandon-release")
            barrier = self.wait_for_either(
                [control / "legacy-owner-removed", control / "kernel-release-pending"], abandoned)
            self.stop_worker(abandoned)
            self.assertTrue(barrier.exists())
            self.assertEqual(
                [item["text"] for item in json.loads(state_path.read_text())["comments"]],
                ["abandoned update"],
            )
            recovery = self.start_lock_worker(root, "recovery", shared_state, control, "recovery", "recover")
            self.finish_worker(recovery, "release recovery")
            persisted = json.loads(state_path.read_text())
            self.assertEqual(sorted(item["text"] for item in persisted["comments"]),
                             ["abandoned update", "recovery update"])


if __name__ == "__main__":
    unittest.main()
