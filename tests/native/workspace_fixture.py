"""Own one disposable native fixture tab inside an existing Herdr workspace."""
import json
import os
import fcntl
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import time
import struct
import termios
import uuid


UNIX_SOCKET_PATH_MAX = 104
TARGET_KEYS = {
    "socket", "workspace_id", "workspace_label", "development_tab_id",
    "development_pane_id", "development_terminal_id", "alacritty_executable",
    "alacritty_pid", "alacritty_start",
}
CALIBRATION_KEY = "capture_calibration"
CALIBRATION_KEYS = {
    "ax_bounds", "cg_window_id", "png_scale", "fixture_layout", "viewport_grid",
    "relative_crop_points",
}
AX_RECT_KEYS = ("X", "Y", "Width", "Height")
GRID_RECT_KEYS = ("x", "y", "width", "height")


class OwnershipError(RuntimeError):
    pass


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def load_target(path):
    target_path = Path(path)
    try:
        value = target_path.lstat()
    except OSError as error:
        raise OwnershipError("target file is unavailable") from error
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o600):
        raise OwnershipError("target file must be a current-user mode-0600 regular file")
    try:
        target = json.loads(target_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise OwnershipError("target file is unreadable or invalid JSON") from error
    if not isinstance(target, dict) or set(target) not in (TARGET_KEYS, TARGET_KEYS | {CALIBRATION_KEY}):
        raise OwnershipError("target file does not match the native target contract")
    strings = TARGET_KEYS - {"alacritty_pid"}
    if any(not isinstance(target[key], str) or not target[key] for key in strings):
        raise OwnershipError("target file has an invalid string value")
    if not isinstance(target["alacritty_pid"], int) or target["alacritty_pid"] <= 0:
        raise OwnershipError("target file has an invalid Alacritty PID")
    if not Path(target["socket"]).is_absolute() or not Path(target["alacritty_executable"]).is_absolute():
        raise OwnershipError("target paths must be absolute")
    if CALIBRATION_KEY in target:
        target[CALIBRATION_KEY] = parse_capture_calibration(target[CALIBRATION_KEY])
    return target


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_rect(value, keys, label, *, positive_size=True):
    if not isinstance(value, dict) or set(value) != set(keys) or any(not is_number(value[key]) for key in keys):
        raise OwnershipError(label + " must be a complete numeric rectangle")
    result = {key: value[key] for key in keys}
    width, height = keys[-2:]
    if positive_size and (result[width] <= 0 or result[height] <= 0):
        raise OwnershipError(label + " must have positive width and height")
    return result


def parse_viewport_grid(value):
    if (not isinstance(value, dict) or set(value) != {"columns", "rows"}
            or any(not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] <= 0
                   for key in ("columns", "rows"))):
        raise OwnershipError("capture calibration viewport_grid must have positive columns and rows")
    return {"columns": value["columns"], "rows": value["rows"]}


def parse_capture_calibration(value):
    if not isinstance(value, dict) or set(value) != CALIBRATION_KEYS:
        raise OwnershipError("capture calibration does not match the native contract")
    if not isinstance(value["cg_window_id"], int) or isinstance(value["cg_window_id"], bool) or value["cg_window_id"] <= 0:
        raise OwnershipError("capture calibration CG window ID is invalid")
    scale = value["png_scale"]
    if (not isinstance(scale, dict) or set(scale) != {"x", "y"}
            or any(not isinstance(scale[key], int) or isinstance(scale[key], bool) or scale[key] <= 0
                   for key in ("x", "y"))):
        raise OwnershipError("capture calibration PNG scale is invalid")
    layout = value["fixture_layout"]
    if not isinstance(layout, dict) or set(layout) != {"area", "pane"}:
        raise OwnershipError("capture calibration fixture layout is invalid")
    calibration = {
        "ax_bounds": parse_rect(value["ax_bounds"], AX_RECT_KEYS, "capture calibration AX bounds"),
        "cg_window_id": value["cg_window_id"],
        "png_scale": {"x": scale["x"], "y": scale["y"]},
        "fixture_layout": {
            "area": parse_rect(layout["area"], GRID_RECT_KEYS, "capture calibration fixture area"),
            "pane": parse_rect(layout["pane"], GRID_RECT_KEYS, "capture calibration fixture pane"),
        },
        "viewport_grid": parse_viewport_grid(value["viewport_grid"]),
        "relative_crop_points": parse_rect(value["relative_crop_points"], AX_RECT_KEYS,
                                            "capture calibration crop"),
    }
    if calibration["relative_crop_points"]["X"] < 0 or calibration["relative_crop_points"]["Y"] < 0:
        raise OwnershipError("capture calibration crop must be relative to the window")
    crop = calibration["relative_crop_points"]
    window = calibration["ax_bounds"]
    if crop["X"] + crop["Width"] > window["Width"] or crop["Y"] + crop["Height"] > window["Height"]:
        raise OwnershipError("capture calibration crop exceeds the AX window")
    pane = calibration["fixture_layout"]["pane"]
    if pane["width"] < calibration["viewport_grid"]["columns"] or pane["height"] < calibration["viewport_grid"]["rows"]:
        raise OwnershipError("capture calibration viewport grid exceeds the fixture pane")
    for point, scale_value in ((calibration["relative_crop_points"]["X"], scale["x"]),
                               (calibration["relative_crop_points"]["Y"], scale["y"]),
                               (calibration["relative_crop_points"]["Width"], scale["x"]),
                               (calibration["relative_crop_points"]["Height"], scale["y"])):
        if point * scale_value != round(point * scale_value):
            raise OwnershipError("capture calibration crop does not map to whole PNG pixels")
    return calibration


def fixture_layout(snapshot, fixture):
    layouts = [layout for layout in snapshot.get("layouts", []) if layout.get("tab_id") == fixture["tab_id"]]
    if len(layouts) != 1:
        raise OwnershipError("recorded fixture layout is missing or ambiguous")
    layout = layouts[0]
    panes = layout.get("panes")
    if (layout.get("workspace_id") != fixture["tab_id"].split(":", 1)[0]
            or layout.get("focused_pane_id") != fixture["pane_id"]
            or not isinstance(panes, list) or len(panes) != 1
            or panes[0].get("pane_id") != fixture["pane_id"]):
        raise OwnershipError("recorded fixture layout changed")
    return {
        "area": parse_rect(layout.get("area"), GRID_RECT_KEYS, "recorded fixture area"),
        "pane": parse_rect(panes[0].get("rect"), GRID_RECT_KEYS, "recorded fixture pane"),
    }


def calibrated_capture_plan(calibration, ax, cg, layout, viewport_grid):
    if calibration is None:
        raise OwnershipError("fixture screenshot is UNVERIFIED: private capture calibration is absent")
    if ax.get("bounds") != calibration["ax_bounds"]:
        raise OwnershipError("fixture screenshot is UNVERIFIED: AX bounds differ from calibration")
    if cg.get("window_id") != calibration["cg_window_id"]:
        raise OwnershipError("fixture screenshot is UNVERIFIED: CoreGraphics window differs from calibration")
    if layout != calibration["fixture_layout"]:
        raise OwnershipError("fixture screenshot is UNVERIFIED: fixture layout differs from calibration")
    if viewport_grid != calibration["viewport_grid"]:
        raise OwnershipError("fixture screenshot is UNVERIFIED: parent viewport grid differs from calibration")
    crop = calibration["relative_crop_points"]
    bounds = ax["bounds"]
    screen = {"X": bounds["X"] + crop["X"], "Y": bounds["Y"] + crop["Y"],
              "Width": crop["Width"], "Height": crop["Height"]}
    if any(value != round(value) for value in screen.values()):
        raise OwnershipError("fixture screenshot is UNVERIFIED: calibrated screen rectangle is not integral")
    pixels = {"width": int(crop["Width"] * calibration["png_scale"]["x"]),
              "height": int(crop["Height"] * calibration["png_scale"]["y"])}
    return {"screen_rectangle_points": {key: int(value) for key, value in screen.items()},
            "expected_pixels": pixels, "bindings": calibration}


def response(value):
    try:
        parsed = json.loads(value)
        return parsed["result"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise OwnershipError("Herdr response lacks result") from error


def process_identity(pid):
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "pid=,lstart=,comm="],
        capture_output=True, text=True, check=True, timeout=3,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    fields = result.stdout.split(None, 7)
    if len(fields) != 7:
        return None
    return {"pid": int(fields[0]), "start": " ".join(fields[1:6]), "executable": fields[6]}


def alacritty_census(executable):
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,lstart=,comm="], capture_output=True, text=True,
        check=True, timeout=3, env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split(None, 7)
        if len(fields) == 7 and fields[6] == executable:
            rows.append({"pid": int(fields[0]), "start": " ".join(fields[1:6])})
    return rows


def socket_owner(socket):
    value = socket.stat()
    if not stat.S_ISSOCK(value.st_mode):
        raise OwnershipError("configured endpoint is not a socket: " + str(socket))
    result = subprocess.run(
        ["/usr/sbin/lsof", "-t", "-nP", "-a", "-U", "--", str(socket)],
        capture_output=True, text=True, timeout=3,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    owners = sorted(int(line) for line in result.stdout.splitlines() if line.isdigit())
    if len(owners) != 1:
        raise OwnershipError("configured socket owner is missing or ambiguous")
    identity = process_identity(owners[0])
    if identity is None:
        raise OwnershipError("configured socket owner disappeared")
    return {"socket": {"device": value.st_dev, "inode": value.st_ino}, "owner": identity}


def runtime_identity(root):
    value = Path(root).lstat()
    if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid()
            or value.st_mode & 0o077):
        raise OwnershipError("native runtime root has unsafe ownership or permissions")
    return {"path": str(Path(root).resolve()), "device": value.st_dev,
            "inode": value.st_ino, "mode": stat.S_IMODE(value.st_mode)}


def nvim_socket_path(root):
    path = Path(root) / "nvim.sock"
    if len(os.fsencode(path)) >= UNIX_SOCKET_PATH_MAX:
        raise OwnershipError("native Neovim socket path exceeds sockaddr_un capacity")
    return path


def select_window(observation, pid, bounds):
    matches = []
    for row in observation.get("windows", []):
        candidate = row.get("bounds")
        if (row.get("pid") == pid and row.get("layer") == 0 and isinstance(candidate, dict)
                and all(isinstance(candidate.get(key), (int, float)) and abs(candidate[key] - bounds[key]) <= 2
                        for key in ("X", "Y", "Width", "Height"))):
            matches.append(row)
    if len(matches) != 1 or not isinstance(matches[0].get("window_id"), int):
        raise OwnershipError("no unique CoreGraphics window matches the existing Alacritty AX window")
    return matches[0]


class ExistingAlacritty:
    """Focus and type in the pre-existing client only after exact PID checks."""
    def __init__(self, target, expected):
        self.target = target
        self.expected = expected

    def verify(self):
        if alacritty_census(self.target["alacritty_executable"]) != self.expected:
            raise OwnershipError("Alacritty census changed")
        current = process_identity(self.target["alacritty_pid"])
        if current != {"pid": self.target["alacritty_pid"], "start": self.target["alacritty_start"],
                       "executable": self.target["alacritty_executable"]}:
            raise OwnershipError("intended Alacritty identity changed")

    def key(self, kind, text=None):
        self.verify()
        if kind == "text":
            if not isinstance(text, str) or not text or not text.isascii() or any(ord(c) < 32 for c in text):
                raise ValueError("native text must be printable ASCII")
            action = "keystroke " + apple_string(text)
        else:
            action = {
                "cmd-enter": "key code 36 using {command down}",
                "ctrl-s": 'keystroke "s" using {control down}',
            }[kind]
        return self._ax(action)

    def _ax(self, action=""):
        script = """
set expectedPid to TARGET_PID
set expectedStart to TARGET_START
if (do shell script \"LC_ALL=C /bin/ps -p \" & expectedPid & \" -o lstart= | /usr/bin/xargs\") is not expectedStart then error \"Alacritty start changed\"
tell application \"System Events\"
  set clients to every application process whose unix id is expectedPid
  if (count of clients) is not 1 then error \"Alacritty AX process missing or ambiguous\"
  set client to item 1 of clients
  tell client
    if unix id is not expectedPid then error \"Alacritty AX PID changed\"
    if (count of windows) is not 1 then error \"Alacritty window set is ambiguous\"
    set frontmost to true
    set targetWindow to window 1
    perform action \"AXRaise\" of targetWindow
    if not frontmost or not (value of attribute \"AXFocused\" of targetWindow) then error \"Alacritty window is not focused\"
    ACTION
    set xy to position of targetWindow
    set wh to size of targetWindow
    return (unix id as text) & \"|\" & (item 1 of xy as text) & \"|\" & (item 2 of xy as text) & \"|\" & (item 1 of wh as text) & \"|\" & (item 2 of wh as text)
  end tell
end tell
""".replace("TARGET_PID", str(self.target["alacritty_pid"])).replace(
            "TARGET_START", apple_string(self.target["alacritty_start"])).replace("ACTION", action)
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                text=True, timeout=10, check=True)
        pid, x, y, width, height = map(float, result.stdout.strip().split("|"))
        if pid != self.target["alacritty_pid"]:
            raise OwnershipError("AX returned another Alacritty PID")
        self.verify()
        return {"pid": int(pid), "bounds": {"X": x, "Y": y, "Width": width, "Height": height}}

    def capture(self, path, helper, layout, viewport_grid):
        receipt = self._ax()
        subprocess.run(["/usr/bin/swiftc", str(Path(__file__).with_name("window_id.swift")), "-o", str(helper)],
                       capture_output=True, text=True, timeout=30, check=True)
        observation = json.loads(subprocess.run([str(helper), str(self.target["alacritty_pid"])], capture_output=True,
                                                text=True, timeout=10, check=True).stdout)
        selected = select_window(observation, self.target["alacritty_pid"], receipt["bounds"])
        plan = calibrated_capture_plan(self.target.get(CALIBRATION_KEY), receipt, selected, layout, viewport_grid)
        rectangle = plan["screen_rectangle_points"]
        region = ",".join(str(rectangle[key]) for key in AX_RECT_KEYS)
        subprocess.run(["/usr/sbin/screencapture", "-x", "-o", "-R", region, str(path)],
                       capture_output=True, text=True, timeout=15, check=True)
        if not path.exists() or path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise OwnershipError("fixture capture did not produce a PNG")
        dimensions = png_dimensions(path)
        if dimensions != plan["expected_pixels"]:
            raise OwnershipError("fixture screenshot dimensions differ from calibration")
        self.verify()
        return {"path": str(path), "ax": receipt, "cg": selected, "dimensions": dimensions,
                "calibrated_capture": plan,
                "screen_capture_preflight": observation.get("screen_capture_preflight")}


def apple_string(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def png_dimensions(path):
    output = subprocess.run(["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                            capture_output=True, text=True, timeout=10, check=True).stdout
    values = {}
    for line in output.splitlines():
        key, separator, value = line.strip().partition(":")
        if separator and key in ("pixelWidth", "pixelHeight"):
            try:
                values[key] = int(value.strip())
            except ValueError as error:
                raise OwnershipError("fixture screenshot dimensions are unreadable") from error
    if set(values) != {"pixelWidth", "pixelHeight"}:
        raise OwnershipError("fixture screenshot dimensions are unreadable")
    return {"width": values["pixelWidth"], "height": values["pixelHeight"]}


class WorkspaceFixture:
    """Create, use, and close one tab whose IDs are the sole mutation authority."""
    def __init__(self, artifact_dir, target, *, runner=None):
        self.artifact = Path(artifact_dir)
        self.artifact.mkdir(parents=True, exist_ok=False)
        self.target = target
        self.socket = Path(target["socket"])
        self.runner = runner or self._run
        self.fixture_root = None
        self.runtime = None
        self.fixture = None
        self.before = None
        self.primary_error = None
        self.retention_reason = None

    def create_runtime_root(self):
        root = Path(tempfile.mkdtemp(prefix="pr00-native-", dir="/tmp")).resolve()
        if root.parent != Path("/tmp").resolve():
            raise OwnershipError("native runtime root is not directly under /tmp")
        self.fixture_root = root
        self.runtime = runtime_identity(root)
        self.runtime["socket"] = str(nvim_socket_path(root))
        self.runtime["socket_bytes"] = len(os.fsencode(nvim_socket_path(root)))
        write_json(self.artifact / "runtime-root.json", self.runtime)
        return root

    def remove_runtime_root(self):
        if self.fixture_root is None:
            return
        if runtime_identity(self.fixture_root) != {key: self.runtime[key] for key in ("path", "device", "inode", "mode")}:
            raise OwnershipError("native runtime root identity changed")
        if self.retention_reason is not None:
            write_json(self.artifact / "runtime-retained.json", {"reason": self.retention_reason,
                       "runtime": self.runtime})
            raise OwnershipError("native runtime retained: " + self.retention_reason)
        import shutil
        shutil.rmtree(self.fixture_root)
        if self.fixture_root.exists():
            raise OwnershipError("native runtime root survived cleanup")

    def retain_runtime(self, reason):
        if not isinstance(reason, str) or not reason:
            raise ValueError("runtime retention reason is required")
        if self.fixture_root is None or self.runtime is None:
            raise OwnershipError("native runtime is unavailable for retention")
        if runtime_identity(self.fixture_root) != {key: self.runtime[key] for key in ("path", "device", "inode", "mode")}:
            raise OwnershipError("native runtime root identity changed")
        self.retention_reason = reason

    def _run(self, args):
        environment = dict(os.environ, HERDR_SOCKET_PATH=str(self.socket))
        result = subprocess.run(["herdr", *args], capture_output=True, text=True, env=environment, timeout=15)
        if result.returncode:
            raise OwnershipError("Herdr command failed: " + " ".join(args) + ": " + result.stderr[-1000:])
        if not result.stdout.strip():
            return {}
        return response(result.stdout)

    def snapshot(self):
        result = self.runner(["api", "snapshot"])
        snapshot = result.get("snapshot")
        if not isinstance(snapshot, dict):
            raise OwnershipError("snapshot result nesting changed")
        return snapshot

    def validate(self, *, require_fixture=False):
        owner = socket_owner(self.socket)
        client = alacritty_census(self.target["alacritty_executable"])
        if client != [{"pid": self.target["alacritty_pid"], "start": self.target["alacritty_start"]}]:
            raise OwnershipError("Alacritty census does not match the approved client")
        snapshot = self.snapshot()
        workspaces = {row.get("workspace_id"): row for row in snapshot.get("workspaces", [])}
        tabs = {row.get("tab_id"): row for row in snapshot.get("tabs", [])}
        panes = {row.get("pane_id"): row for row in snapshot.get("panes", [])}
        if workspaces.get(self.target["workspace_id"], {}).get("label") != self.target["workspace_label"]:
            raise OwnershipError("approved workspace missing or ambiguous")
        dev = panes.get(self.target["development_pane_id"])
        if (tabs.get(self.target["development_tab_id"], {}).get("workspace_id") != self.target["workspace_id"]
                or dev is None or dev.get("tab_id") != self.target["development_tab_id"]
                or dev.get("terminal_id") != self.target["development_terminal_id"]):
            raise OwnershipError("development tab/pane identity changed")
        if require_fixture:
            if self.fixture is None:
                raise OwnershipError("fixture has not been created")
            tab = tabs.get(self.fixture["tab_id"])
            pane = panes.get(self.fixture["pane_id"])
            if (tab is None or tab.get("workspace_id") != self.target["workspace_id"] or pane is None
                    or pane.get("tab_id") != self.fixture["tab_id"]
                    or pane.get("terminal_id") != self.fixture["terminal_id"]):
                raise OwnershipError("recorded fixture identity changed")
            fixture_panes = {row.get("pane_id") for row in snapshot.get("panes", [])
                             if row.get("tab_id") == self.fixture["tab_id"]}
            if fixture_panes != {self.fixture["pane_id"]}:
                raise OwnershipError("unowned pane appeared in fixture tab")
        if self.before is not None and owner != self.before["socket"]:
            raise OwnershipError("configured socket owner changed from the pre-create identity")
        return {"socket": owner, "alacritty": client, "snapshot": snapshot}

    def create(self):
        self.before = self.validate()
        self.create_runtime_root()
        label = "PR00 fixture " + uuid.uuid4().hex
        env = [
            "HOME=" + str(self.fixture_root / "home"),
            "XDG_CONFIG_HOME=" + str(self.fixture_root / "config"),
            "XDG_CACHE_HOME=" + str(self.fixture_root / "cache"),
            "XDG_STATE_HOME=" + str(self.fixture_root / "state"),
            "XDG_DATA_HOME=" + str(self.fixture_root / "data"),
            "XDG_RUNTIME_DIR=" + str(self.fixture_root / "runtime"),
            "HERDR_BIN_PATH=/usr/bin/false",
        ]
        for item in env:
            Path(item.split("=", 1)[1]).mkdir(parents=True, exist_ok=True) if item.split("=", 1)[0] != "HERDR_BIN_PATH" else None
        result = self.runner(["tab", "create", "--workspace", self.target["workspace_id"], "--cwd", str(self.fixture_root),
                              "--label", label, *sum((["--env", item] for item in env), []), "--focus"])
        try:
            tab, pane = result["tab"], result["root_pane"]
            self.fixture = {"tab_id": tab["tab_id"], "pane_id": pane["pane_id"],
                            "terminal_id": pane["terminal_id"], "label": label}
        except (KeyError, TypeError) as error:
            raise OwnershipError("tab create did not return tab and root pane identities") from error
        if not self.fixture["tab_id"].startswith(self.target["workspace_id"] + ":") or not self.fixture["pane_id"].startswith(self.target["workspace_id"] + ":"):
            raise OwnershipError("tab create returned another workspace")
        checked = self.validate(require_fixture=True)
        if checked["snapshot"].get("focused_tab_id") != self.fixture["tab_id"]:
            raise OwnershipError("fixture tab did not become focused")
        write_json(self.artifact / "fixture-created.json", {"before": self.before, "runtime": self.runtime,
                   "fixture": self.fixture, "checked": checked})
        return self.fixture

    def run(self, command):
        self.validate(require_fixture=True)
        self.runner(["pane", "run", self.fixture["pane_id"], command])

    def focus_for_input(self):
        self.validate(require_fixture=True)
        self.runner(["tab", "focus", self.fixture["tab_id"]])
        checked = self.fixture_focus()
        return ExistingAlacritty(self.target, checked["alacritty"])

    def fixture_focus(self):
        checked = self.validate(require_fixture=True)
        snapshot = checked["snapshot"]
        if snapshot.get("focused_tab_id") != self.fixture["tab_id"] or snapshot.get("focused_pane_id") != self.fixture["pane_id"]:
            raise OwnershipError("server focus is not the recorded fixture pane")
        return checked

    def parent_pty_grid(self):
        self.fixture_focus()
        info = self.runner(["pane", "process-info", "--pane", self.fixture["pane_id"]])
        process = info.get("process_info", {})
        pid = process.get("shell_pid")
        if not isinstance(pid, int) or pid <= 0:
            raise OwnershipError("fixture parent PTY owner is unavailable")
        output = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "0", "-Fn"],
                                capture_output=True, text=True, timeout=3, check=True).stdout.splitlines()
        terminals = [line[1:] for line in output if line.startswith("n/dev/tty")]
        if len(terminals) != 1:
            raise OwnershipError("fixture parent PTY is missing or ambiguous")
        with open(terminals[0], "rb", buffering=0) as terminal:
            rows, columns, _, _ = struct.unpack("HHHH", fcntl.ioctl(terminal.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        if rows <= 0 or columns <= 0:
            raise OwnershipError("fixture parent PTY has no grid")
        return {"columns": columns, "rows": rows, "tty": terminals[0], "shell_pid": pid}

    def key(self, kind, text=None):
        client = self.focus_for_input()
        self.fixture_focus()
        receipt = client.key(kind, text)
        checked = self.validate(require_fixture=True)
        if checked["snapshot"].get("focused_pane_id") != self.fixture["pane_id"]:
            raise OwnershipError("fixture focus changed during native input")
        return receipt

    def capture(self, name="fixture.png", *, viewport_grid):
        if Path(name).name != name or not name.endswith(".png"):
            raise ValueError("capture must use a PNG basename")
        client = self.focus_for_input()
        checked = self.fixture_focus()
        layout = fixture_layout(checked["snapshot"], self.fixture)
        receipt = client.capture(self.artifact / name, self.artifact / "window-id", layout, parse_viewport_grid(viewport_grid))
        checked = self.validate(require_fixture=True)
        if checked["snapshot"].get("focused_pane_id") != self.fixture["pane_id"]:
            raise OwnershipError("fixture focus changed during capture")
        write_json(self.artifact / (name + ".json"), receipt)
        return receipt

    def close(self):
        cleanup_error = None
        try:
            if self.fixture is not None:
                checked = self.validate(require_fixture=True)
                panes = [row for row in checked["snapshot"].get("panes", []) if row.get("tab_id") == self.fixture["tab_id"]]
                if {row.get("pane_id") for row in panes} != {self.fixture["pane_id"]}:
                    raise OwnershipError("unowned pane appeared in fixture tab")
                prior_tab = self.before["snapshot"].get("focused_tab_id")
                if checked["snapshot"].get("focused_tab_id") == self.fixture["tab_id"] and prior_tab:
                    self.runner(["tab", "focus", prior_tab])
                self.validate(require_fixture=True)
                self.runner(["tab", "close", self.fixture["tab_id"]])
                after = self.validate()
                if any(row.get("tab_id") == self.fixture["tab_id"] for row in after["snapshot"].get("tabs", [])):
                    raise OwnershipError("fixture tab survived close")
                if after["socket"] != self.before["socket"] or after["alacritty"] != self.before["alacritty"]:
                    raise OwnershipError("pre-existing socket owner or Alacritty changed")
                write_json(self.artifact / "fixture-teardown.json", {"status": "PASS", "after": after})
                self.fixture = None
            self.remove_runtime_root()
        except BaseException as error:
            cleanup_error = error
            write_json(self.artifact / "fixture-teardown.json", {"status": "FAIL", "error": str(error)})
        if self.primary_error is not None and cleanup_error is not None:
            raise self.primary_error from cleanup_error
        if cleanup_error is not None:
            raise cleanup_error

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.primary_error = exc
        self.close()
        return False


def shell_command(argv):
    return shlex.join([str(item) for item in argv])
