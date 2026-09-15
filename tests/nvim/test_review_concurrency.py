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
        })
        if scenario:
            values["HERDR_REVIEW_SCENARIO"] = scenario
        return values

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
                    "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l",
                    str(HERE / "review_concurrency_worker.lua"),
                ], cwd=REPO, env=self.environment(root, "reader", shared_state, control, "reader", scenario),
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    self.wait_for(control / "reader-ready", reader)
                    writer = subprocess.run([
                        "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l",
                        str(HERE / "review_concurrency_worker.lua"),
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


if __name__ == "__main__":
    unittest.main()
