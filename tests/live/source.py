"""Exact source provenance, independently of the current instrumentation tree."""
import contextlib
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile


def git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.check_output(["git", *args], cwd=root, env=env, text=True, stderr=subprocess.PIPE, timeout=10).strip()


def exact_revision(root, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be an exact 40-character commit SHA")
    actual = git(root, "rev-parse", "--verify", revision + "^{commit}")
    if actual != revision:
        raise ValueError("revision does not identify the exact commit")
    return {"revision": revision, "tree": git(root, "rev-parse", revision + "^{tree}")}


def checked_source(root, revision):
    provenance = exact_revision(root, revision)
    if git(root, "rev-parse", "HEAD") != revision:
        raise ValueError("revision is not checked-out HEAD")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("refusing dirty tracked or untracked checkout")
    return {**provenance, "mode": "clean-checkout"}


def new_artifact(path):
    path = Path(path).absolute()
    # mkdir is exclusive, including dangling symlinks. No deletion on failure.
    path.mkdir(parents=True, exist_ok=False)
    return path


@contextlib.contextmanager
def source_archive(repo, revision):
    provenance = exact_revision(repo, revision)
    directory = Path(tempfile.mkdtemp(prefix="hf-", dir="/tmp"))
    try:
        archive = directory / "source.tar"
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        with archive.open("xb") as output:
            subprocess.run(["git", "archive", "--format=tar", revision], cwd=repo,
                           env=env, stdout=output, check=True, timeout=30)
        provenance.update(mode="exact-git-archive", archive_directory=str(directory), archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        tree = directory / "source"
        tree.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(tree, filter="data")
        yield tree, provenance
    except BaseException:
        provenance["retain_archive"] = True
        raise
    finally:
        if not provenance.get("retain_archive", False):
            shutil.rmtree(directory)
