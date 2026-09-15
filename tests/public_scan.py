#!/usr/bin/env python3
"""Behavioral checks for the redacted candidate-tree and history scanner."""

import os
import pathlib
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "check-public.sh"


def run(args, cwd, **kwargs):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False, **kwargs)


def repo(directory):
    path = pathlib.Path(directory)
    path.mkdir()
    assert run(["git", "init", "-q"], path).returncode == 0
    assert run(["git", "config", "user.email", "fixture@example.invalid"], path).returncode == 0
    assert run(["git", "config", "user.name", "Fixture"], path).returncode == 0
    (path / "safe.txt").write_text("synthetic fixture only\n")
    assert run(["git", "add", "safe.txt"], path).returncode == 0
    commit(path, "safe fixture")
    return path


def commit(path, message):
    result = run(["git", "commit", "-m", message], path)
    assert result.returncode == 0, result.stderr


def scan(path, **env):
    return run([str(SCANNER)], path, env={**os.environ, "PUBLIC_SCAN_ROOT": str(path), **env})


def assert_pass(path, **env):
    result = scan(path, **env)
    assert result.returncode == 0 and result.stdout == "public audit: ok\n", result.stderr


def assert_redacted_failure(path, secret, **env):
    result = scan(path, **env)
    output = result.stdout + result.stderr
    assert result.returncode != 0 and secret not in output, output


with tempfile.TemporaryDirectory(prefix="public-scan-test-") as directory:
    root = pathlib.Path(directory)

    # An outgoing commit is explicit: unstaged and staged changes are not HEAD.
    candidate_repo = repo(root / "candidate")
    canary = "PUBLIC-SCAN-" + "CANARY-CANDIDATE"
    (candidate_repo / "candidate.txt").write_text(canary + "\n")
    assert_pass(candidate_repo)
    assert run(["git", "add", "candidate.txt"], candidate_repo).returncode == 0
    assert_pass(candidate_repo)
    index_tree = run(["git", "write-tree"], candidate_repo).stdout.strip()
    assert_redacted_failure(candidate_repo, canary, PUBLIC_SCAN_CANDIDATE=index_tree, PUBLIC_SCAN_HISTORY_RANGE="HEAD")
    commit(candidate_repo, "candidate canary")
    assert_redacted_failure(candidate_repo, canary, PUBLIC_SCAN_CANDIDATE="HEAD", PUBLIC_SCAN_HISTORY_RANGE="HEAD")

    metadata_repo = repo(root / "metadata")
    metadata_canary = "PUBLIC-SCAN-" + "CANARY-METADATA"
    (metadata_repo / "safe.txt").write_text("still safe\n")
    assert run(["git", "add", "safe.txt"], metadata_repo).returncode == 0
    commit(metadata_repo, metadata_canary)
    assert_redacted_failure(metadata_repo, metadata_canary, PUBLIC_SCAN_HISTORY_RANGE="HEAD")

    history_repo = repo(root / "history")
    (history_repo / "id_rsa").write_text("harmless fixture contents\n")
    assert run(["git", "add", "id_rsa"], history_repo).returncode == 0
    commit(history_repo, "sensitive filename")
    assert run(["git", "rm", "-q", "id_rsa"], history_repo).returncode == 0
    commit(history_repo, "remove sensitive filename")
    assert_redacted_failure(history_repo, "id_rsa", PUBLIC_SCAN_HISTORY_RANGE="HEAD")

    deny_repo = repo(root / "denylist")
    private_entry = "fixture.private[scanner]"
    (deny_repo / "scripts").mkdir()
    (deny_repo / "scripts" / "check-public.sh").write_text(private_entry + "\n")
    assert run(["git", "add", "scripts/check-public.sh"], deny_repo).returncode == 0
    commit(deny_repo, "scanner fixture")
    denylist = root / "private-denylist"
    denylist.write_text(private_entry + "\n")
    assert_redacted_failure(deny_repo, private_entry, PUBLIC_SCAN_PRIVATE_DENYLIST=str(denylist), PUBLIC_SCAN_HISTORY_RANGE="HEAD")
    for kind in ("binary", "filename", "symlink"):
        target = repo(root / kind)
        filename = "nested/" + (private_entry if kind == "filename" else "fixture")
        (target / filename).parent.mkdir()
        if kind == "symlink":
            (target / filename).symlink_to(private_entry)
        else:
            (target / filename).write_bytes(b"safe" if kind == "filename" else b"\0" + private_entry.encode() + b"\xff")
        assert run(["git", "add", filename], target).returncode == 0
        commit(target, "synthetic blob and path coverage")
        assert_redacted_failure(target, private_entry, PUBLIC_SCAN_PRIVATE_DENYLIST=str(denylist), PUBLIC_SCAN_HISTORY_RANGE="HEAD")
        assert run(["git", "rm", "--", filename], target).returncode == 0
        commit(target, "remove synthetic finding")
        assert_redacted_failure(target, private_entry, PUBLIC_SCAN_PRIVATE_DENYLIST=str(denylist), PUBLIC_SCAN_HISTORY_RANGE="HEAD")
    assert_redacted_failure(deny_repo, "missing-candidate", PUBLIC_SCAN_CANDIDATE="missing-candidate")
    assert_redacted_failure(deny_repo, "does-not-exist", PUBLIC_SCAN_HISTORY_RANGE="does-not-exist")
    assert_redacted_failure(deny_repo, "missing-denylist", PUBLIC_SCAN_PRIVATE_DENYLIST=str(root / "missing"))

print("public scanner tests: ok")
