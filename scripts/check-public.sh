#!/usr/bin/env bash
set -euo pipefail
export PUBLIC_SCAN_ROOT=${PUBLIC_SCAN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
exec python3 - "$@" <<'PY'
import os
from pathlib import Path
import re
import subprocess
import sys


def fail(category):
    print("public audit failed: " + category, file=sys.stderr)


def scan():
    candidate = os.environ.get("PUBLIC_SCAN_CANDIDATE", "HEAD")
    history = os.environ.get("PUBLIC_SCAN_HISTORY_RANGE", "--all")
    denylist = os.environ.get("PUBLIC_SCAN_PRIVATE_DENYLIST", "")
    args = iter(sys.argv[1:])
    for arg in args:
        value = next(args, None)
        if value is None:
            raise ValueError()
        if arg == "--candidate":
            candidate = value
        elif arg == "--history-range":
            history = value
        elif arg == "--denylist":
            denylist = value
        else:
            raise ValueError()
    root = os.environ["PUBLIC_SCAN_ROOT"]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, env=environment, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60).stdout
    git("rev-parse", "--is-inside-work-tree")
    entries = [entry for entry in Path(denylist).read_bytes().splitlines() if entry] if denylist else []
    canary = b"PUBLIC-SCAN-" + b"CANARY-[A-Z0-9_-]+"
    content = re.compile(b"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{20,}|" + canary)
    sensitive_path = re.compile(rb"(^|/)(\.env($|\.)|id_(rsa|ed25519)($|\.)|[^/]*\.(pem|p12|key)$)")
    def prohibited(data):
        return bool(content.search(data)) or any(entry in data for entry in entries)
    blobs = {}
    trees = {}
    def inspect_tree(tree):
        if tree in trees:
            return trees[tree]
        categories = set()
        for entry in git("ls-tree", "-rz", "-r", tree).split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            _, kind, oid = header.split()
            if prohibited(path) or sensitive_path.search(path):
                categories.add("sensitive filename")
            if kind == b"blob":
                if oid not in blobs:
                    blobs[oid] = prohibited(git("cat-file", "blob", oid.decode("ascii")))
                if blobs[oid]:
                    categories.add("prohibited content")
        trees[tree] = categories
        return categories
    failed = False
    def report(categories, location):
        nonlocal failed
        for category in sorted(categories):
            fail(category + " in " + location)
            failed = True
    tree = git("rev-parse", "--verify", "--end-of-options", candidate + "^{tree}").strip().decode("ascii")
    report(inspect_tree(tree), "candidate tree")
    # A candidate may be an exact index tree; commit candidates include metadata.
    obj = git("rev-parse", "--verify", "--end-of-options", candidate + "^{}").strip().decode("ascii")
    if git("cat-file", "-t", obj).strip() == b"commit":
        if prohibited(git("cat-file", "commit", obj)):
            report({"prohibited content"}, "candidate metadata")
    if history.startswith("-") and history != "--all":
        raise ValueError()
    for commit in git("rev-list", history).splitlines():
        commit = commit.decode("ascii")
        tree = git("rev-parse", "--verify", commit + "^{tree}").strip().decode("ascii")
        report(inspect_tree(tree), "selected Git history")
        if prohibited(git("cat-file", "commit", commit)):
            report({"prohibited content"}, "selected Git metadata")
    if not failed:
        print("public audit: ok")
    return int(failed)


try:
    sys.exit(scan())
except Exception:
    # Git diagnostics, paths and denylist values must never reach public output.
    fail("scanner input or Git object unavailable")
    sys.exit(1)
PY
