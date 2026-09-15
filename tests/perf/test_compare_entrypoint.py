import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "tests/live"))
from runtime_lease import LEASE_FIELDS, RuntimeLease, require_runtime_lease

require_runtime_lease(repo=REPO)


class PublicCompareEntrypointTests(unittest.TestCase):
    def environment(self, *, inherited=True):
        values = dict(os.environ)
        if not inherited:
            for key in LEASE_FIELDS:
                values.pop(key, None)
        values["PYTHONDONTWRITEBYTECODE"] = "1"
        return values

    def make_repository(self, parent):
        target = parent / "repository"
        shutil.copytree(REPO, target, ignore=shutil.ignore_patterns(".git", "artifacts", "__pycache__"))
        subprocess.run(["git", "init", "--quiet"], cwd=target, check=True)
        subprocess.run(["git", "config", "user.email", "compare@example.invalid"], cwd=target, check=True)
        subprocess.run(["git", "config", "user.name", "Compare Fixture"], cwd=target, check=True)
        subprocess.run(["git", "add", "."], cwd=target, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=target, check=True)
        return target, subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()

    def command(self, repo, revision, artifact, scenario="shell-open"):
        return [
            str(repo / "tests/perf/compare"), "--scenario", scenario,
            "--baseline", revision, "--candidate", revision,
            "--samples", "1", "--artifact-dir", str(artifact),
        ]

    def run_public(self, repo, revision, artifact, *, environment, scenario="shell-open"):
        return subprocess.run(
            self.command(repo, revision, artifact, scenario), cwd=repo, env=environment,
            text=True, capture_output=True, timeout=20,
        )

    def assert_fake_measurement(self, artifact):
        result = json.loads((artifact / "result.json").read_text())
        self.assertEqual([item["status"] for item in result["observations"]], ["PASS", "PASS"])
        self.assertTrue(all((path / "model.json").exists() for path in artifact.glob("sample-*")))

    def test_absent_lease_is_owned_for_the_public_fake_smoke_and_released(self):
        with tempfile.TemporaryDirectory(prefix="herdr-compare-public-", dir="/tmp") as temporary:
            repo, revision = self.make_repository(Path(temporary))
            clean = self.environment(inherited=False)
            artifact = Path(temporary) / "success"
            result = self.run_public(repo, revision, artifact, environment=clean)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_fake_measurement(artifact)
            with RuntimeLease.acquire_owner(repo=repo, inherited=clean) as lease:
                self.assertTrue(lease.owner)

    def test_conflicting_owner_refuses_before_artifact_or_runtime_creation(self):
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
        with tempfile.TemporaryDirectory(prefix="herdr-compare-conflict-", dir="/tmp") as temporary:
            artifact = Path(temporary) / "must-not-exist"
            result = self.run_public(REPO, revision, artifact, environment=self.environment(inherited=False))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("runtime lease is held", result.stderr)
            self.assertFalse(artifact.exists())

    def test_valid_inherited_owner_is_borrowed_for_nested_fake_work(self):
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
        with tempfile.TemporaryDirectory(prefix="herdr-compare-inherited-", dir="/tmp") as temporary:
            artifact = Path(temporary) / "success"
            result = self.run_public(REPO, revision, artifact, environment=self.environment())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_fake_measurement(artifact)

    def test_invalid_and_stale_inherited_authority_refuse(self):
        with tempfile.TemporaryDirectory(prefix="herdr-compare-authority-", dir="/tmp") as temporary:
            repo, revision = self.make_repository(Path(temporary))
            clean = self.environment(inherited=False)
            invalid = dict(clean)
            invalid.update({key: "invalid" for key in LEASE_FIELDS})
            first = Path(temporary) / "invalid"
            result = self.run_public(repo, revision, first, environment=invalid)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("runtime lease environment is invalid", result.stderr)
            self.assertFalse(first.exists())

            lease = RuntimeLease.acquire_owner(repo=repo, inherited=clean)
            stale = lease.child_environment(clean)
            lease.close()
            second = Path(temporary) / "stale"
            result = self.run_public(repo, revision, second, environment=stale)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stale, released, or forged", result.stderr)
            self.assertFalse(second.exists())

    def test_failure_path_releases_public_owner(self):
        with tempfile.TemporaryDirectory(prefix="herdr-compare-failure-", dir="/tmp") as temporary:
            repo, revision = self.make_repository(Path(temporary))
            clean = self.environment(inherited=False)
            artifact = Path(temporary) / "failure"
            result = self.run_public(repo, revision, artifact, environment=clean, scenario="unsupported")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(artifact.exists())
            with RuntimeLease.acquire_owner(repo=repo, inherited=clean) as lease:
                self.assertTrue(lease.owner)


if __name__ == "__main__":
    unittest.main()
