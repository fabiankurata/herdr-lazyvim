"""One owned runtime and one Herdr boot/teardown path for live workloads.

Only mkdtemp-created runtime paths are deleted. Evidence gets a new child under
artifact_dir so importing prototypes cannot overwrite earlier ownership records.
"""
import contextlib
import hashlib
import json
import math
import os
import platform
import re
import sys
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import threading
import uuid

from runtime_lease import require_runtime_lease, preserve_lease_environment


class OwnershipError(RuntimeError):
    pass


class _BoundedOutput:
    """Drain a child stream while retaining only its most recent bytes."""
    limit = 65536

    def __init__(self):
        self.data = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def append(self, value):
        with self.lock:
            self.total += len(value)
            self.data.extend(value)
            if len(self.data) > self.limit:
                del self.data[:-self.limit]

    def text(self):
        with self.lock:
            return bytes(self.data).decode(errors="replace")


def _drain_output(stream, retained):
    try:
        while True:
            value = stream.read(8192)
            if not value:
                return
            retained.append(value)
    except (OSError, ValueError):
        return


def public_diagnostic(text, environment, limit=16384):
    """Return bounded child output with inherited control values removed."""
    value = text or ""
    secrets_to_remove = []
    for key, item in environment.items():
        if key in {"HOME", "ZDOTDIR"} or key.startswith(("HERDR", "NVIM", "XDG_")):
            if isinstance(item, str) and len(item) >= 4:
                secrets_to_remove.append((key, item))
    for key, item in sorted(secrets_to_remove, key=lambda pair: len(pair[1]), reverse=True):
        value = value.replace(item, "<redacted:" + key + ">")
    value = re.sub(r"(?i)((?:token|password|secret)\s*[=:]\s*)\S+", r"\1<redacted>", value)
    if len(value) > limit:
        half = (limit - len("\n... diagnostics truncated ...\n")) // 2
        value = value[:half] + "\n... diagnostics truncated ...\n" + value[-half:]
    return value


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def isolated_environment(root, inherited=None):
    inherited = os.environ if inherited is None else inherited
    env = {
        key: value for key, value in inherited.items()
        if not key.startswith(("HERDR", "NVIM", "XDG_", "GIT_"))
        and key not in {"HOME", "ZDOTDIR", "ENV", "BASH_ENV", "VIMINIT", "EXINIT"}
    }
    directories = {
        "HOME": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_STATE_HOME": "state",
        "XDG_DATA_HOME": "data",
        "XDG_RUNTIME_DIR": "runtime",
        "XDG_CONFIG_DIRS": "config-dirs",
        "XDG_DATA_DIRS": "data-dirs",
        "ZDOTDIR": "home",
    }
    for key, directory in directories.items():
        path = root / directory
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(path)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               PYTHONDONTWRITEBYTECODE="1")
    env.update(preserve_lease_environment(inherited))
    return env


def process_table():
    """Read identities only: never argv, environment, buffers, or user files."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,comm="],
        capture_output=True, text=True, check=True, timeout=3,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    records = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 9)
        if len(fields) == 10:
            pid, ppid, pgid, state = fields[:4]
            records[int(pid)] = {
                "pid": int(pid), "ppid": int(ppid), "pgid": int(pgid),
                "state": state, "start": " ".join(fields[4:9]),
                "executable": fields[9],
            }
    return records


def same_process(left, right):
    # exec() can change comm; start time plus a still-owned PID is the identity.
    return right is not None and left["pid"] == right["pid"] and left["start"] == right["start"]


def exception_evidence(exc):
    """Bounded exception graph, including explicit causes and implicit cleanup context."""
    nodes = []
    seen = {}
    def visit(error):
        if error is None:
            return None
        if id(error) in seen:
            return seen[id(error)]
        if len(nodes) >= 32:
            return "truncated"
        index = len(nodes)
        seen[id(error)] = index
        node = {"type": type(error).__name__, "message": str(error)[:4096]}
        nodes.append(node)
        node["cause"] = visit(error.__cause__)
        node["context"] = visit(error.__context__)
        return index
    visit(exc)
    return {"error": "\n".join(node["type"] + ": " + node["message"] for node in nodes),
            "exceptions": nodes}


def socket_identity(path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    identity = {"device": value.st_dev, "inode": value.st_ino,
                "mode": value.st_mode, "ctime_ns": value.st_ctime_ns}
    if stat.S_ISLNK(value.st_mode):
        endpoint = path.resolve()
        identity["resolved_path"] = str(endpoint)
        identity["target"] = socket_identity(endpoint)
    return identity


def fixture_identities(table):
    """Read private supervisor receipts, accepting only current process incarnations."""
    selected = set()
    for path in Path("/tmp").glob("hf-*/supervisor.json"):
        try:
            root = path.parent.lstat()
            value = path.lstat()
            if (root.st_uid != os.getuid() or root.st_mode & 0o077
                    or not stat.S_ISDIR(root.st_mode) or value.st_uid != os.getuid()
                    or not stat.S_ISREG(value.st_mode) or value.st_mode & 0o077):
                continue
            receipt = json.loads(path.read_text())
            if not same_process(receipt["supervisor"], table.get(receipt["supervisor"]["pid"])):
                continue
            selected.add(receipt["supervisor"]["pid"])
            for item in receipt["processes"]:
                if same_process(item, table.get(item["pid"])):
                    selected.add(item["pid"])
        except (FileNotFoundError, ProcessLookupError):
            continue
    while True:
        children = {pid for pid, item in table.items() if item["ppid"] in selected}
        if children <= selected:
            return selected
        selected.update(children)


def default_identities(inherited):
    home = Path(inherited.get("HOME", str(Path.home())))
    config = Path(inherited.get("XDG_CONFIG_HOME", str(home / ".config")))
    paths = {home / ".config/herdr/herdr.sock", config / "herdr/herdr.sock"}
    for key in ("HERDR_SOCKET_PATH", "NVIM", "NVIM_LISTEN_ADDRESS"):
        if inherited.get(key):
            paths.add(Path(inherited[key]))
    identities = {str(path): socket_identity(path) for path in sorted(paths)}
    owners = set()
    for path, identity in identities.items():
        endpoint = identity.get("target") if identity and "target" in identity else identity
        if endpoint is None or not stat.S_ISSOCK(endpoint["mode"]):
            continue
        path = identity.get("resolved_path", path)
        result = subprocess.run(["lsof", "-t", "-nP", "-a", "-U", "--", path],
                                capture_output=True, text=True, timeout=3)
        if result.returncode not in (0, 1):
            raise OwnershipError("cannot observe default socket owner")
        owners.update(int(line) for line in result.stdout.splitlines() if line.isdigit())
    processes = process_table()
    selected = {pid for pid in owners if pid in processes}
    fixtures = fixture_identities(processes)
    for pid, record in processes.items():
        if pid in fixtures or Path(record["executable"].strip("()")).name != "nvim":
            continue
        parent = record["ppid"]
        visited = set()
        while parent in processes and parent not in visited:
            if parent in owners:
                selected.add(pid)
                break
            visited.add(parent)
            ancestor = processes[parent]
            parent = ancestor["ppid"]
    return {
        "sockets": identities,
        "socket_owner_pids": sorted(owners),
        "processes": {str(pid): processes[pid] for pid in sorted(selected)},
        "scope": "default/inherited socket owners and their Neovim descendants, excluding verified fixture supervisors and owned descendants",
    }


@contextlib.contextmanager
def defer_cancellation():
    """Defer Python cancellation without passing a blocked signal mask to exec."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending = []
    def defer(signum, frame):
        pending.append(signum)
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, defer)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        for signum in pending:
            signal.raise_signal(signum)


class OwnedSession:
    def __init__(self, artifact_dir, *, real=True, session=None, socket=None,
                 startup_timeout=5, rpc_timeout=3, teardown_timeout=5,
                 disable_update_checks=False):
        for duration in (startup_timeout, rpc_timeout, teardown_timeout):
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                raise ValueError("runtime deadlines must be finite positive seconds")
        self.artifact = Path(artifact_dir)
        self.root = None
        self.socket = None
        self.session = None
        self.herdr = None
        self.proc = None
        self.env = None
        self.real = real
        self.requested_session = session
        self.requested_socket = socket
        self.startup_timeout = startup_timeout
        self.rpc_timeout = rpc_timeout
        self.teardown_timeout = teardown_timeout
        self.disable_update_checks = disable_update_checks
        self._inherited = dict(os.environ)
        self._root = None
        self._root_identity = None
        self._session = None
        self._socket = None
        self._env = None
        self._children = []
        self._groups = set()
        self._identities = {}
        self._before = None
        self._evidence = None
        self._server_log = None
        self._closed = False
        self._retain_root_reason = None
        self._events = []
        self._previous_sigterm = None
        self._signal_installed = False
        self._lease = None
        self._lease_environment = None

    def _write_update_control(self):
        """Write the supported update controls inside this owned runtime only."""
        if not self.disable_update_checks:
            return
        config_home = Path(self._env["XDG_CONFIG_HOME"])
        expected = self._root.resolve() / config_home.relative_to(self._root)
        if config_home.resolve() != expected:
            raise OwnershipError("update control must stay inside owned XDG_CONFIG_HOME")
        path = config_home / "herdr" / "config.toml"
        path.parent.mkdir(mode=0o700)
        content = "[update]\nversion_check = false\nmanifest_check = false\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
        path.chmod(0o600)
        write_json(self._evidence / "update-control.json", {
            "path": str(path),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
            "applied": {"version_check": False, "manifest_check": False},
        })

    def _event(self, event, **details):
        self._events.append({"event": event, "monotonic": time.monotonic(), **details})
        if self._evidence is not None:
            write_json(self._evidence / "events.json", self._events)

    def _validate_target(self, args=(), *, environment=None, workload=False):
        environment = self.env if environment is None else environment
        if self._root is None or self.root != self._root:
            raise OwnershipError("runtime is not owned")
        current = self._root.lstat()
        if (current.st_dev, current.st_ino) != self._root_identity or not stat.S_ISDIR(current.st_mode):
            raise OwnershipError("runtime identity changed")
        if self.session != self._session or self.socket != self._socket:
            raise OwnershipError("target must equal the generated owned session and socket")
        if self.requested_session not in (None, self._session):
            raise OwnershipError("caller session is not the generated owned session")
        if self.requested_socket is not None and str(self.requested_socket) != str(self._socket):
            raise OwnershipError("caller socket is not the generated owned socket")
        if self._socket.resolve() != self._root.resolve() / self._socket.relative_to(self._root):
            raise OwnershipError("socket parent redirects outside the owned runtime")
        for key, value in self._env.items():
            if key in {"HOME", "ZDOTDIR"} or key.startswith(("HERDR", "XDG_")):
                if environment.get(key) != value:
                    raise OwnershipError("isolated environment changed: " + key)
        for key, value in self._env.items():
            if key in {"HOME", "ZDOTDIR"} or key.startswith("XDG_"):
                expected = self._root.resolve() / Path(value).relative_to(self._root)
                if Path(value).resolve() != expected:
                    raise OwnershipError("isolated directory redirects outside runtime: " + key)
        workload_variables = {
            "HERDR_BIN_PATH", "HERDR_PLUGIN_ROOT", "HERDR_PLUGIN_CONFIG_DIR",
            "HERDR_PLUGIN_CONTEXT_JSON", "HERDR_LAZYVIM_SLEEP_BIN", "HERDR_TEST_MODEL",
            "HERDR_TEST_CALLS", "HERDR_TEST_STATE_ROOT", "HERDR_TEST_AGENT_BYTES",
        } if workload else set()
        for key in environment:
            if key.startswith(("HERDR", "NVIM", "XDG_")) and key not in self._env and key not in workload_variables:
                raise OwnershipError("inherited routing/configuration variable: " + key)
        for key in ("ENV", "BASH_ENV", "VIMINIT", "EXINIT"):
            if key in environment:
                raise OwnershipError("executable initialization variable: " + key)
        for arg in args:
            if str(arg).split("=", 1)[0] in {"--session", "--socket", "--socket-path", "--config", "--config-dir", "--remote", "--remote-keybindings"}:
                raise OwnershipError("caller may not override owned routing/configuration")
        if args and args[0] not in {"--version", "--help", "status", "api", "workspace", "tab", "pane", "agent", "notification", "server"}:
            raise OwnershipError("command is outside the owned server API")
        if args and args[0] == "server" and tuple(args) != ("server", "stop"):
            raise OwnershipError("only owned server stop is a public server command")
        if self.herdr != self._herdr:
            raise OwnershipError("Herdr executable changed")

    def _remember(self):
        running = {child.pid for child in self._children if child.poll() is None}
        table = process_table()
        # A PGID is authority only while a verified incarnation anchors it.
        conflicts = {pid for pid, item in self._identities.items()
                     if pid in table and not same_process(item, table[pid])}
        found = {pid for pid, item in self._identities.items()
                 if same_process(item, table.get(pid))}
        found.update(running - conflicts)
        anchored_groups = {table[pid]["pgid"] for pid in found if pid in table}
        anchored_groups.update(running - conflicts)
        self._groups.intersection_update(anchored_groups)
        found.update(pid for pid, item in table.items()
                     if item["pgid"] in self._groups and pid not in conflicts)
        while True:
            descendants = {pid for pid, item in table.items()
                           if item["ppid"] in found and pid not in conflicts}
            if descendants <= found:
                break
            found.update(descendants)
        for pid in found:
            if pid in table:
                self._identities.setdefault(pid, table[pid])
        self._publish_supervisor()
        return table

    def _publish_supervisor(self):
        if self._root is None or not hasattr(self, "_supervisor"):
            return
        temporary = self._root / "supervisor.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"supervisor": self._supervisor,
                       "processes": list(self._identities.values())}, stream)
        temporary.replace(self._root / "supervisor.json")

    def _spawn(self, argv, **kwargs):
        with defer_cancellation():
            environment = kwargs.pop("env", self.env)
            started = kwargs.pop("_started", None)
            child = subprocess.Popen(argv, env=environment, cwd=self._root,
                                     start_new_session=True, **kwargs)
            self._children.append(child)
            self._groups.add(child.pid)
            if started is not None:
                started(child)
        self._remember()
        return child

    def execute(self, argv, *, env=None, check=True, timeout=None):
        """Supervise a workload child in this runtime, including on cancellation."""
        self._validate_target()
        if self._closed:
            raise OwnershipError("owned runtime is closed")
        environment = self.env if env is None else env
        self._validate_target(environment=environment, workload=True)
        if str(argv[0]) == self.herdr:
            if list(argv[1:3]) != ["--session", self.session]:
                raise OwnershipError("Herdr child must use the generated owned session")
            self._validate_target(argv[3:])
        duration = self.rpc_timeout if timeout is None else timeout
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("child deadline must be finite positive seconds")
        deadline = time.monotonic() + duration
        diagnostic = self._evidence / ("child-" + uuid.uuid4().hex)
        diagnostic.mkdir(mode=0o700)
        stdout_retained, stderr_retained = _BoundedOutput(), _BoundedOutput()
        readers = []
        child = None

        def finish_diagnostic(status):
            for reader in readers:
                reader.join(timeout=2)
            stdout, stderr = stdout_retained.text(), stderr_retained.text()
            for name, value in (("stdout.txt", stdout), ("stderr.txt", stderr)):
                descriptor = os.open(diagnostic / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(value)
            write_json(diagnostic / "result.json", {
                "status": status,
                "stage": Path(str(argv[0])).name,
                "argument_count": len(argv),
                "returncode": child.returncode if child is not None else None,
                "stdout_bytes_seen": stdout_retained.total,
                "stderr_bytes_seen": stderr_retained.total,
                "stdout_truncated": stdout_retained.total > stdout_retained.limit,
                "stderr_truncated": stderr_retained.total > stderr_retained.limit,
            })
            return stdout, stderr

        try:
            def start_readers(process):
                nonlocal child
                child = process
                for stream, retained in ((child.stdout, stdout_retained), (child.stderr, stderr_retained)):
                    reader = threading.Thread(target=_drain_output, args=(stream, retained), daemon=True)
                    reader.start()
                    readers.append(reader)
            child = self._spawn(
                argv, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                _started=start_readers,
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, duration)
                try:
                    child.wait(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    self._remember()
            self._remember()
        except BaseException as primary:
            cleanup = None
            try:
                self.close()
            except BaseException as error:
                cleanup = error
            status = "TIMEOUT" if isinstance(primary, subprocess.TimeoutExpired) else (
                "CANCELLED" if isinstance(primary, KeyboardInterrupt) else "FAILED"
            )
            stdout, stderr = finish_diagnostic(status)
            if isinstance(primary, subprocess.TimeoutExpired):
                failure = subprocess.TimeoutExpired(argv, duration, output=stdout, stderr=stderr)
                failure.__cause__ = primary
            else:
                failure = primary
                failure.stdout, failure.stderr = stdout, stderr
            failure.stage = Path(str(argv[0])).name
            failure.diagnostic_path = str(diagnostic)
            if cleanup is not None:
                primary.__context__ = cleanup
            raise failure
        stdout, stderr = finish_diagnostic("PASS" if child.returncode == 0 else "NONZERO")
        result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
        result.stage = Path(str(argv[0])).name
        result.diagnostic_path = str(diagnostic)
        if check:
            if result.returncode:
                failure = subprocess.CalledProcessError(
                    result.returncode, result.args, output=result.stdout, stderr=result.stderr,
                )
                failure.stage = result.stage
                failure.diagnostic_path = result.diagnostic_path
                raise failure
        return result

    def run(self, *args, check=True):
        self._validate_target(args)
        if self._closed or not self.real:
            raise OwnershipError("no active real runtime")
        started = time.monotonic()
        result = self.execute([self.herdr, "--session", self.session, *args], check=False)
        self._event("rpc", args=list(args), exit_code=result.returncode,
                    elapsed_ms=(time.monotonic() - started) * 1000)
        if check:
            result.check_returncode()
        return result

    def __enter__(self):
        if self._root is not None or self._closed:
            raise OwnershipError("owned context cannot be reused")
        if threading.current_thread() is not threading.main_thread():
            raise OwnershipError("owned sessions require the main thread for cancellation handling")
        try:
            self._lease_environment = {key: os.environ.get(key) for key in preserve_lease_environment(os.environ)}
            self._lease = require_runtime_lease()
            def interrupted(signum, frame):
                raise KeyboardInterrupt("owned runtime received signal " + str(signum))
            self._previous_sigterm = signal.signal(signal.SIGTERM, interrupted)
            self._signal_installed = True
            with defer_cancellation():
                self.root = Path(tempfile.mkdtemp(prefix="hf-", dir="/tmp"))
                self._root = self.root
                value = self.root.lstat()
                self._root_identity = (value.st_dev, value.st_ino)
            self.session = "codex-pr00-" + uuid.uuid4().hex[:12]
            self._session = self.session
            self.socket = self.root / "config/herdr/sessions" / self.session / "herdr.sock"
            self._socket = self.socket
            self.env = isolated_environment(self.root, self._inherited)
            self.env["HERDR_SOCKET_PATH"] = str(self.socket)
            self._env = dict(self.env)
            self.herdr = shutil.which("herdr", path=self.env.get("PATH"))
            self._herdr = self.herdr
            self._validate_target()
            self.artifact.mkdir(parents=True, exist_ok=True)
            self._evidence = self.artifact / ("owned-session-" + uuid.uuid4().hex)
            self._evidence.mkdir()
            self._supervisor = process_table()[os.getpid()]
            self._publish_supervisor()
            self._before = default_identities(self._inherited)
            write_json(self._evidence / "default-before.json", self._before)
            write_json(self._evidence / "manifest.json", {
                "root": str(self.root), "session": self.session, "socket": str(self.socket),
                "real": self.real, "herdr": self.herdr,
                "python": sys.version, "platform": platform.platform(),
                "environment": {key: value for key, value in self._env.items()
                                if key == "HOME" or key.startswith(("XDG_", "HERDR"))},
                "timeouts_seconds": {"startup": self.startup_timeout, "rpc": self.rpc_timeout,
                                     "teardown": self.teardown_timeout},
            })
            self._write_update_control()
            if not self.real:
                return self
            if self.herdr is None:
                raise FileNotFoundError("installed Herdr CLI unavailable")
            version = self.run("--version")
            (self._evidence / "versions.txt").write_text(version.stdout + version.stderr)
            if self.socket.exists() or self.socket.parent.exists():
                self._retain_root_reason = "preexisting session paths were not adopted; runtime retained"
                raise OwnershipError(self._retain_root_reason)
            before = self.run("status", "server", check=False)
            (self._evidence / "status-before.txt").write_text(before.stdout + before.stderr)
            if before.returncode or f"socket: {self.socket}" not in before.stdout or "status: not running" not in before.stdout:
                self._retain_root_reason = "preexisting or incorrectly routed server was not adopted; runtime retained"
                raise OwnershipError(self._retain_root_reason)
            self._validate_target()
            if self.socket.exists():
                self._retain_root_reason = "socket appeared before owned boot; runtime retained"
                raise OwnershipError(self._retain_root_reason)
            self._server_log = (self._evidence / "server.txt").open("x")
            # Assign proc in the signal-protected region as well as registering it.
            with defer_cancellation():
                self.proc = self._spawn([self.herdr, "--session", self.session, "server"],
                                        stdout=self._server_log, stderr=subprocess.STDOUT)
            self._event("server-started", pid=self.proc.pid)
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    self._retain_root_reason = "foreground server exited before readiness; runtime retained for unknown daemon state"
                    raise OwnershipError(self._retain_root_reason)
                if self.socket.exists():
                    snapshot = self.run("api", "snapshot", check=False)
                    if snapshot.returncode == 0:
                        observation = json.loads(snapshot.stdout)
                        if not isinstance(observation, dict) or not isinstance(observation.get("result"), dict) or "error" in observation:
                            raise OwnershipError("snapshot did not contain a successful API result")
                        (self._evidence / "snapshot.json").write_text(snapshot.stdout)
                        self._event("ready")
                        return self
                time.sleep(0.05)
            raise TimeoutError("owned server startup deadline")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        try:
            with defer_cancellation():
                self._close()
        finally:
            if self._lease is not None:
                self._lease.close()
                self._lease = None
            if self._lease_environment is not None:
                for key, value in self._lease_environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                self._lease_environment = None
            if self._signal_installed:
                signal.signal(signal.SIGTERM, self._previous_sigterm)
                self._signal_installed = False

    def _close(self):
        errors = [self._retain_root_reason] if self._retain_root_reason else []
        signal_errors = []
        observation_failed = False

        def observe():
            nonlocal observation_failed
            try:
                return self._remember()
            except Exception as exc:
                observation_failed = True
                if not errors:
                    errors.append("process observation failed: " + str(exc))
                return {}

        # Remember PTY descendants before stopping their foreground parent.
        table = observe()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            live = [item for pid, item in self._identities.items()
                    if same_process(item, table.get(pid)) and not table[pid]["state"].startswith("Z")]
            parents = {item["ppid"] for item in live}
            live.sort(key=lambda item: item["pid"] in parents)
            for item in live:
                try:
                    current = observe().get(item["pid"])
                    if not same_process(item, current):
                        continue
                    os.kill(item["pid"], sig)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    signal_errors.append({"pid": item["pid"], "signal": int(sig), "error": str(exc)})
            # Handles cover failure between Popen and identity observation. A
            # running direct child still owns the new group created at spawn.
            for child in self._children:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, sig)
                    except ProcessLookupError:
                        pass
                    except OSError as exc:
                        signal_errors.append({"group": child.pid, "signal": int(sig), "error": str(exc)})
            deadline = time.monotonic() + self.teardown_timeout / 2
            while time.monotonic() < deadline:
                for child in self._children:
                    child.poll()
                table = observe()
                alive = [pid for pid, item in self._identities.items()
                         if same_process(item, table.get(pid))]
                if not alive and all(child.poll() is not None for child in self._children):
                    break
                time.sleep(0.05)
            table = observe()
        remaining = [item for pid, item in self._identities.items()
                     if same_process(item, table.get(pid))]
        for child in self._children:
            try:
                child.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                errors.append("unreaped child: " + str(child.pid))
        if remaining:
            errors.append("owned descendants still present; runtime retained")
        for child in self._children:
            if child.returncode is not None:
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
        if self._server_log is not None:
            self._server_log.close()
        unchanged = None
        if self._before is not None:
            try:
                after = default_identities(self._inherited)
                unchanged = self._before["sockets"] == after["sockets"] and all(
                    same_process(item, after["processes"].get(pid))
                    for pid, item in self._before["processes"].items()
                )
                if not unchanged:
                    errors.append("preexisting default/editor identities changed")
                write_json(self._evidence / "default-after.json", after)
            except Exception as exc:
                errors.append("default identity observation failed: " + str(exc))
        removed = False
        if self._root is not None and not remaining and not errors and not observation_failed:
            try:
                value = self._root.lstat()
                if stat.S_ISDIR(value.st_mode) and (value.st_dev, value.st_ino) == self._root_identity:
                    shutil.rmtree(self._root)
                    removed = True
                else:
                    errors.append("runtime directory identity changed; retained")
            except Exception as exc:
                errors.append("runtime removal failed: " + str(exc))
        if self._evidence is not None:
            write_json(self._evidence / "teardown.json", {
                "status": "FAIL" if errors else "PASS", "errors": errors,
                "signal_errors": signal_errors,
                "owned_processes": list(self._identities.values()), "remaining": remaining,
                "children_reaped": all(child.returncode is not None for child in self._children),
                "default_identities_unchanged": unchanged, "runtime_removed": removed,
                "process_observation_complete": not observation_failed,
            })
        self._closed = True
        if errors:
            raise OwnershipError("; ".join(errors))

    def __exit__(self, *unused):
        self.close()
