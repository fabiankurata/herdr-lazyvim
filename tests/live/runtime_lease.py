"""POSIX runtime-lease ownership and supervised Python process control."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time

LEASE_TOKEN = "HERDR_RUNTIME_LEASE_TOKEN"
LEASE_PID = "HERDR_RUNTIME_LEASE_PID"
LEASE_START = "HERDR_RUNTIME_LEASE_START"
LEASE_PATH = "HERDR_RUNTIME_LEASE_PATH"
LEASE_FIELDS = frozenset((LEASE_TOKEN, LEASE_PID, LEASE_START, LEASE_PATH))

class RuntimeLeaseError(RuntimeError): pass
class RuntimeLeaseBusy(RuntimeLeaseError): pass

def _process_identity(pid):
    if not isinstance(pid, int) or pid <= 0: return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "pid=,ppid=,stat=,lstart="], capture_output=True, text=True, check=True, timeout=3, env={"PATH": os.defpath, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError): return None
    values = result.stdout.split()
    if len(values) != 8 or not values[0].isdigit() or not values[1].isdigit() or values[2].startswith("Z"): return None
    return {"pid": int(values[0]), "ppid": int(values[1]), "start": " ".join(values[3:])}

def _group_identities(group):
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,ppid=,stat=,lstart="], capture_output=True, text=True,
                                check=True, timeout=3, env={"PATH": os.defpath, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeLeaseError("cannot observe recorded child group") from error
    members = {}
    for line in result.stdout.splitlines():
        values = line.split()
        if len(values) == 9 and values[0].isdigit() and values[1].isdigit() and values[2].isdigit() and int(values[1]) == group and not values[3].startswith("Z"):
            members[int(values[0])] = {"pid": int(values[0]), "ppid": int(values[2]), "start": " ".join(values[4:])}
    return members

def _same_incarnation(left, right):
    return left is not None and right is not None and left["pid"] == right["pid"] and left["start"] == right["start"]

def _is_ancestor(holder, child_pid):
    seen = set()
    while child_pid not in seen:
        seen.add(child_pid); current = _process_identity(child_pid)
        if current is None: return False
        if current["pid"] == holder["pid"] and current["start"] == holder["start"]: return True
        if current["ppid"] <= 1: return False
        child_pid = current["ppid"]
    return False

def _git_common_dir(repo):
    root = Path(repo or Path.cwd()).resolve(strict=True)
    try: value = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--git-common-dir"], text=True, timeout=3, env={"PATH": os.defpath, "LC_ALL": "C"}).strip()
    except (OSError, subprocess.SubprocessError) as error: raise RuntimeLeaseError("cannot locate canonical Git common directory") from error
    path = Path(value); return (root / path if not path.is_absolute() else path).resolve(strict=True)

def lease_path(repo=None):
    common = _git_common_dir(repo)
    suffix = hashlib.sha256((str(common) + "\0" + str(os.getuid())).encode()).hexdigest()[:20]
    return common / ("herdr-runtime-" + str(os.getuid()) + "-" + suffix + ".lock")

def _lock_file(path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    value = os.fstat(descriptor)
    if value.st_uid != os.getuid() or not stat.S_ISREG(value.st_mode) or value.st_mode & 0o077:
        os.close(descriptor); raise RuntimeLeaseError("runtime lease file is not a private regular file")
    os.fchmod(descriptor, 0o600); return descriptor

def _read_record(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    try: value = json.loads(os.read(descriptor, 65536))
    except (TypeError, ValueError) as error: raise RuntimeLeaseError("runtime lease holder metadata is unreadable") from error
    if not isinstance(value, dict): raise RuntimeLeaseError("runtime lease holder metadata has invalid shape")
    return value

class RuntimeLease:
    def __init__(self, descriptor, path, holder, token, *, owner):
        self._descriptor, self.path, self.holder, self.token = descriptor, Path(path), holder, token
        self.owner, self._closed = owner, False
    @classmethod
    def borrow(cls, *, repo=None, inherited=None):
        inherited = os.environ if inherited is None else inherited; values = {key: inherited.get(key) for key in LEASE_FIELDS}
        if any(not isinstance(value, str) or not value for value in values.values()): raise RuntimeLeaseError("runtime lease is required from an explicit owner wrapper")
        path = lease_path(repo)
        if values[LEASE_PATH] != str(path) or not values[LEASE_PID].isdigit(): raise RuntimeLeaseError("runtime lease environment is invalid")
        descriptor = _lock_file(path)
        try:
            try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: held = True
            else: held = False; fcntl.flock(descriptor, fcntl.LOCK_UN)
            record = _read_record(descriptor)
        finally: os.close(descriptor)
        holder = record.get("holder")
        valid = (held and isinstance(holder, dict) and set(holder) == {"pid", "ppid", "start"} and isinstance(record.get("token"), str) and isinstance(record.get("path"), str) and record["path"] == str(path) and secrets.compare_digest(record["token"], values[LEASE_TOKEN]) and _process_identity(holder.get("pid")) == holder and str(holder["pid"]) == values[LEASE_PID] and holder["start"] == values[LEASE_START] and _is_ancestor(holder, os.getpid()))
        if not valid: raise RuntimeLeaseError("runtime lease holder identity is stale, released, or forged")
        return cls(None, path, holder, values[LEASE_TOKEN], owner=False)
    @classmethod
    def acquire_owner(cls, *, repo=None, inherited=None):
        inherited = os.environ if inherited is None else inherited
        if any(inherited.get(key) is not None for key in LEASE_FIELDS): return cls.borrow(repo=repo, inherited=inherited)
        path = lease_path(repo); descriptor = _lock_file(path)
        try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor); raise RuntimeLeaseBusy("runtime lease is held; refuse before starting runtime resources") from error
        holder = _process_identity(os.getpid())
        if holder is None: os.close(descriptor); raise RuntimeLeaseError("cannot record current runtime lease holder identity")
        token = secrets.token_urlsafe(32); record = {"version": 2, "path": str(path), "token": token, "holder": holder, "acquired_at": time.time()}
        os.ftruncate(descriptor, 0); os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode()); os.fsync(descriptor)
        return cls(descriptor, path, holder, token, owner=True)
    acquire = acquire_owner
    def child_environment(self, environment=None):
        value = dict(os.environ if environment is None else environment); value.update({LEASE_TOKEN: self.token, LEASE_PID: str(self.holder["pid"]), LEASE_START: self.holder["start"], LEASE_PATH: str(self.path)}); return value
    def install(self): os.environ.update(self.child_environment()); return self
    def close(self):
        if not self._closed:
            self._closed = True
            if self.owner: fcntl.flock(self._descriptor, fcntl.LOCK_UN); os.close(self._descriptor)
    def __enter__(self): return self
    def __exit__(self, *unused): self.close()

def require_runtime_lease(*, repo=None): return RuntimeLease.borrow(repo=repo)
@contextlib.contextmanager
def runtime_lease_owner(*, repo=None):
    previous = {key: os.environ.get(key) for key in LEASE_FIELDS}; lease = RuntimeLease.acquire_owner(repo=repo).install()
    try: yield lease
    finally:
        lease.close()
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
def preserve_lease_environment(inherited): return {key: inherited[key] for key in LEASE_FIELDS if key in inherited}

def _git_state(repo):
    root = Path(repo).resolve(strict=True)
    def git(*args): return subprocess.check_output(["git", "-C", str(root), *args], text=True, timeout=3).strip()
    return {"candidate": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}
def _remove_control_root(path, identity):
    try:
        value = Path(path).lstat()
        if (value.st_dev, value.st_ino) != identity or value.st_uid != os.getuid() or not stat.S_ISDIR(value.st_mode): return
        for item in Path(path).iterdir(): item.unlink()
        Path(path).rmdir()
    except FileNotFoundError: pass

class RecordedRun:
    """Live in-memory supervisor; JSON receipts cannot authorize a signal."""
    def __init__(self, path, process, record, lease, root, listener):
        self.path, self.process, self.record, self.lease = Path(path), process, record, lease; self.root, self.listener = Path(root), listener
        value = self.root.lstat(); self.root_identity, self.closed, self.stopped_at = (value.st_dev, value.st_ino), False, None
        self.lifecycle, self.group_members = threading.Lock(), {process.pid: record["child"]}
        self.thread = threading.Thread(target=self._serve, daemon=True); self.thread.start()
    @classmethod
    def start(cls, path, command, *, repo, environment=None, stdout=None, stderr=None):
        lease, process, root, listener = RuntimeLease.acquire_owner(repo=repo), None, None, None
        try:
            path = Path(path); path.parent.mkdir(parents=True, exist_ok=True); root = Path(tempfile.mkdtemp(prefix="hr-", dir="/tmp")); root.chmod(0o700)
            endpoint = root / "control.sock"; listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); listener.bind(str(endpoint)); endpoint.chmod(0o600); listener.listen(4); listener.settimeout(.1)
            process = subprocess.Popen(command, cwd=repo, env=lease.child_environment(environment), start_new_session=True, stdout=stdout, stderr=stderr)
            identity = _process_identity(process.pid)
            if identity is None: raise RuntimeLeaseError("cannot record child process identity")
            control = {"socket": str(endpoint), "run_id": secrets.token_urlsafe(24), "token": secrets.token_urlsafe(32)}
            record = {"version": 2, "status": "RUNNING", "command": list(command), "repo": str(Path(repo).resolve()), "lease": {"path": str(lease.path), "holder": lease.holder}, "child": identity, "group": process.pid, "started_at": time.time(), "git": _git_state(repo), "control": control}
            path.write_text(json.dumps(record, indent=2) + "\n"); path.chmod(0o600); return cls(path, process, record, lease, root, listener)
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
            if listener is not None: listener.close()
            if root is not None:
                value = root.lstat(); _remove_control_root(root, (value.st_dev, value.st_ino))
            lease.close(); raise
    def _write(self): self.path.write_text(json.dumps(self.record, indent=2) + "\n"); self.path.chmod(0o600)
    def _stop_owned(self):
        with self.lifecycle:
            if self.closed or self.process.poll() is not None or _process_identity(self.process.pid) != self.record["child"]: raise RuntimeLeaseError("supervisor no longer owns a live recorded child")
            members = _group_identities(self.process.pid)
            if members.get(self.process.pid) != self.record["child"]: raise RuntimeLeaseError("supervisor cannot validate its recorded child group")
            self.group_members.update(members); self.stopped_at = time.time(); os.killpg(self.process.pid, signal.SIGTERM)
            deadline = time.monotonic() + .75
            while time.monotonic() < deadline and _group_identities(self.process.pid): time.sleep(.05)
            members = _group_identities(self.process.pid)
            if members:
                if any(not _same_incarnation(self.group_members.get(pid), identity) for pid, identity in members.items()): raise RuntimeLeaseError("recorded child group identity changed")
                os.killpg(self.process.pid, signal.SIGKILL)
                deadline = time.monotonic() + .75
                while time.monotonic() < deadline and _group_identities(self.process.pid): time.sleep(.05)
                if _group_identities(self.process.pid): raise RuntimeLeaseError("recorded child group survived bounded stop")
    def _serve(self):
        while not self.closed:
            try: connection, _ = self.listener.accept()
            except TimeoutError: continue
            except OSError: return
            with connection:
                try:
                    connection.settimeout(.5); request = json.loads(connection.recv(4096)); control = self.record["control"]
                    if not isinstance(request, dict) or set(request) != {"run_id", "token"}: raise RuntimeLeaseError("invalid stop request")
                    if not isinstance(request["run_id"], str) or not isinstance(request["token"], str): raise RuntimeLeaseError("invalid stop request")
                    if not (secrets.compare_digest(request["run_id"], control["run_id"]) and secrets.compare_digest(request["token"], control["token"])): raise RuntimeLeaseError("stop request is not authorized")
                    self._stop_owned(); reply = {"status": "accepted"}
                except (OSError, ValueError, RuntimeLeaseError) as error: reply = {"status": "refused", "error": str(error)}
                try: connection.sendall(json.dumps(reply).encode())
                except OSError: pass
    def finish(self):
        code = self.process.wait()
        with self.lifecycle:
            members = _group_identities(self.process.pid)
            if members:
                self.record.update(status="UNRESOLVED", exit_code=code, finished_at=time.time(), unresolved_group=list(members.values()))
                self._write(); raise RuntimeLeaseError("recorded child group remains active")
            self.record.update(status="STOPPED" if self.stopped_at else "COMPLETE", exit_code=code, finished_at=time.time())
            if self.stopped_at: self.record["stopped_at"] = self.stopped_at
            self._write()
        self.close()
        return code
    def close(self):
        if not self.closed:
            self.closed = True; self.listener.close(); self.thread.join(timeout=1); _remove_control_root(self.root, self.root_identity); self.lease.close()

def stop_recorded_run(path, *, timeout=3):
    """Request a stop from the original supervisor; never signal receipt PIDs."""
    record = json.loads(Path(path).read_text()); control = record.get("control")
    if not isinstance(control, dict) or set(control) != {"socket", "run_id", "token"} or not all(isinstance(v, str) and v for v in control.values()): raise RuntimeLeaseError("recorded run has no live supervisor control handle")
    if len(os.fsencode(control["socket"])) >= 104: raise RuntimeLeaseError("recorded supervisor socket path is invalid")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); client.settimeout(timeout)
    try:
        client.connect(control["socket"]); client.sendall(json.dumps({"run_id": control["run_id"], "token": control["token"]}).encode()); reply = json.loads(client.recv(4096))
    except (OSError, ValueError) as error: raise RuntimeLeaseError("original recorded supervisor is unavailable") from error
    finally: client.close()
    if not isinstance(reply, dict) or reply.get("status") != "accepted": raise RuntimeLeaseError("original recorded supervisor refused stop request")
