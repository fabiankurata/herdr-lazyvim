#!/usr/bin/env python3
"""Run the PR01 comment-operation review in an approved existing workspace.

The runner has no source-only fallback.  It records UNVERIFIED when a caller has
not supplied the private calibrated target that the native fixture requires.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
LIVE = HERE.parent / "live"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(LIVE) not in sys.path:
    sys.path.append(str(LIVE))

from source import checked_source, new_artifact, source_archive
from workspace_fixture import OwnershipError, WorkspaceFixture, load_target, nvim_socket_path, shell_command, write_json

BASELINE = "488f4c543ed7ba0937b8a8725ef438b79112c800"
LANES = (
    ("root-switch", "root-switch.png"),
    ("composer-root", "composer-root.png"),
    ("new-comment", "new-comment.png"),
    ("edited-comment", "edited-comment.png"),
    ("cancel-send", "cancel-send.png"),
    ("send-failure", "send-failure.png"),
    ("uncertain", "uncertain.png"),
    ("closed-origin", "closed-origin.png"),
    ("restart", "restart.png"),
    ("batch", "batch.png"),
)
NVIM_APPNAME = "herdr-pr01-review"
DIAGNOSTIC_LIMIT = 12_000


def wait_for(probe, predicate, label, timeout=10):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if predicate(last):
                return last
        except Exception as error:
            last = {"error": str(error)}
        time.sleep(.05)
    raise OwnershipError(label + "; last observation: " + json.dumps(last, sort_keys=True))


def remote(nvim, socket, lua):
    command = [nvim, "--server", str(socket), "--remote-expr", "luaeval(" + json.dumps(lua) + ")"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=4, check=True)
    return json.loads(completed.stdout)


def scenario_fixture(repo, revision, runtime):
    """Extract the exact committed fixture beside the archived product source."""
    destination = runtime / "comment_operations_fixture.lua"
    fixture = "tests/native/comment_operations_fixture.lua"
    completed = subprocess.run(["git", "-C", str(repo), "show", revision + ":" + fixture],
                               capture_output=True, check=True)
    destination.write_bytes(completed.stdout)
    return destination, hashlib.sha256(destination.read_bytes()).hexdigest()


def init_file(runtime, source, fixture, socket):
    init = runtime / "comment_operations_init.lua"
    init.write_text(
        "vim.g.mapleader = ' '\n"
        "vim.opt.runtimepath:prepend(" + json.dumps(str(source / "nvim")) + ")\n"
        "vim.env.HERDR_BIN_PATH = '/usr/bin/false'\n"
        "vim.env.HERDR_WORKSPACE_ID = 'fixture-workspace'\n"
        "vim.env.HERDR_PANE_ID = 'fixture-editor'\n"
        "vim.env.HERDR_TEST_FIXTURE_ROOT = " + json.dumps(str(runtime)) + "\n"
        "vim.env.HERDR_TEST_STATE_ROOT = vim.fn.stdpath('state')\n"
        "require('herdr_lazyvim').setup()\n"
        "dofile(" + json.dumps(str(fixture)) + ")\n"
        "vim.g.pr01_native_socket = " + json.dumps(str(socket)) + "\n"
    )
    return init


def isolated_nvim_environment(runtime):
    """Create the disposable Neovim home before Neovim resolves stdpath()."""
    roots = {
        "HOME": runtime / "home",
        "XDG_CONFIG_HOME": runtime / "config",
        "XDG_DATA_HOME": runtime / "data",
        "XDG_STATE_HOME": runtime / "state",
        "XDG_CACHE_HOME": runtime / "cache",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({key: str(path) for key, path in roots.items()})
    environment["NVIM_APPNAME"] = NVIM_APPNAME
    return environment


def native_nvim_command(nvim, socket, init, native_file):
    """Start with the fixture's isolated XDG roots and an explicit app name."""
    return ["env", "NVIM_APPNAME=" + NVIM_APPNAME, nvim, "--listen", socket,
            "-u", init, "-i", "NONE", native_file]


def bounded_output(value):
    if not value:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    if len(value) <= DIAGNOSTIC_LIMIT:
        return value
    return value[:DIAGNOSTIC_LIMIT] + "\n[output truncated]\n"


def write_headless_report(artifact, *, command, completed=None, error=None, lanes=None):
    """Persist bounded Neovim output and the fixture's last lane observations."""
    stderr = artifact / "headless-smoke-stderr"
    stderr.mkdir()
    stdout = "" if completed is None else completed.stdout
    stderr_output = "" if completed is None else completed.stderr
    returncode = "not started" if completed is None else str(completed.returncode)
    if isinstance(error, subprocess.CalledProcessError):
        stdout = error.stdout if error.stdout is not None else stdout
        stderr_output = error.stderr if error.stderr is not None else stderr_output
        returncode = str(error.returncode)
    elif error is not None:
        stdout = getattr(error, "stdout", None) or getattr(error, "output", None) or stdout
        stderr_output = getattr(error, "stderr", None) or stderr_output
    command_log = "command: " + shlex.join([str(item) for item in command]) + "\nreturncode: " + returncode + "\n"
    if stdout:
        command_log += "\nstdout:\n" + bounded_output(stdout)
    if stderr_output:
        command_log += "\nstderr:\n" + bounded_output(stderr_output)
    (stderr / "command.log").write_text(command_log)
    if lanes is not None:
        write_json(artifact / "headless-smoke-lanes.json", lanes)
    report = ["# PR01 headless smoke", "", "- status: " + ("PASS" if error is None else "FAIL"),
              "- return code: " + returncode, "- command log: `headless-smoke-stderr/command.log`"]
    if lanes is not None:
        report.append("- lane diagnostics: `headless-smoke-lanes.json`")
    if error is not None:
        report.append("- error: " + str(error))
    (artifact / "report.md").write_text("\n".join(report) + "\n")


def read_headless_lanes(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        return {"status": "UNAVAILABLE", "error": str(error)}


class ReviewVideo:
    """Make a review video from fresh, calibrated native captures."""
    def __init__(self, review, fixture, viewport_grid):
        self.review, self.fixture, self.viewport_grid = Path(review), fixture, viewport_grid
        self.frames = []

    def capture(self, label):
        name = "review-frame-%02d-%s.png" % (len(self.frames), label)
        receipt = self.fixture.capture(name, viewport_grid=self.viewport_grid)
        path = self.review / name
        self.frames.append({"path": str(path), "receipt": receipt,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "captured_at": time.monotonic(), "label": label})

    def capture_for(self, label, seconds=3):
        """Sample fresh frames during a real interval without PNG reuse."""
        deadline = time.monotonic() + seconds
        while True:
            self.capture(label)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1, remaining))

    @staticmethod
    def review_excerpt(frames):
        """Select an actual 30–60 second interval containing a composer save."""
        interactions = ("composer-root", "new-comment", "edited-comment", "closed-origin", "restart")
        for interaction in interactions:
            opened = interaction + "-opened"
            saved = interaction + "-saved"
            for start_index, start in enumerate(frames):
                if start["label"] != opened:
                    continue
                save_index = next((index for index in range(start_index + 1, len(frames))
                                   if frames[index]["label"] == saved), None)
                if save_index is None:
                    continue
                valid_ends = [index for index in range(save_index, len(frames))
                              if 30 <= frames[index]["captured_at"] - start["captured_at"] <= 60]
                if valid_ends:
                    end_index = valid_ends[-1]
                    return {"interaction": interaction, "start_index": start_index, "end_index": end_index}

    @staticmethod
    def write_concat(path, frames):
        rows = ["ffconcat version 1.0"]
        for frame, following in zip(frames, frames[1:]):
            frame_duration = following["captured_at"] - frame["captured_at"]
            rows.extend(("file '" + frame["path"].replace("'", "'\\\\''") + "'", "duration %.6f" % frame_duration))
        if frames:
            rows.append("file '" + frames[-1]["path"].replace("'", "'\\\\''") + "'")
        path.write_text("\n".join(rows) + "\n")

    def finish(self):
        full_duration = (self.frames[-1]["captured_at"] - self.frames[0]["captured_at"]
                         if len(self.frames) >= 2 else 0)
        full_session = {
            "frame_count": len(self.frames), "duration_seconds": full_duration, "frames": self.frames,
            "frame_cadence_seconds": [self.frames[index + 1]["captured_at"] - frame["captured_at"]
                                      for index, frame in enumerate(self.frames[:-1])],
        }
        self.write_concat(self.review / "review-full-session.ffconcat", self.frames)
        selection = self.review_excerpt(self.frames)
        if selection is None:
            receipt = {"status": "UNVERIFIED", "full_session": full_session,
                       "review_excerpt": {"status": "UNVERIFIED",
                                          "reason": "no contiguous 30–60 second composer interaction excerpt"}}
            write_json(self.review / "review-video.json", receipt)
            return receipt
        excerpt = self.frames[selection["start_index"]:selection["end_index"] + 1]
        duration = excerpt[-1]["captured_at"] - excerpt[0]["captured_at"]
        manifest = self.review / "review-excerpt.ffconcat"
        self.write_concat(manifest, excerpt)
        output = self.review / "review.mp4"
        subprocess.run(["/opt/homebrew/bin/ffmpeg", "-y", "-safe", "0", "-f", "concat", "-i", str(manifest),
                        "-vsync", "vfr", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)],
                       capture_output=True, text=True, timeout=90, check=True)
        receipt = {"status": "PASS", "full_session": full_session,
                   "review_excerpt": {"status": "PASS", **selection, "start_offset_seconds": excerpt[0]["captured_at"] - self.frames[0]["captured_at"],
                                      "end_offset_seconds": excerpt[-1]["captured_at"] - self.frames[0]["captured_at"],
                                      "duration_seconds": duration, "frame_count": len(excerpt), "output": str(output)}}
        write_json(self.review / "review-video.json", receipt)
        return receipt


def run_headless_smoke(repo, revision, artifact_dir):
    """Drive the public fixture phases without an owned terminal or native-key claim."""
    artifact = new_artifact(artifact_dir)
    nvim = shutil.which("nvim")
    if nvim is None:
        error = OwnershipError("nvim is unavailable")
        write_headless_report(artifact, command=["nvim"], error=error)
        raise error
    with tempfile.TemporaryDirectory(prefix="pr01-headless-", dir="/tmp") as temporary:
        runtime = Path(temporary)
        with source_archive(repo, revision) as (source, _):
            fixture, _ = scenario_fixture(repo, revision, runtime)
            init = init_file(runtime, source, fixture, runtime / "nvim.sock")
            script = runtime / "smoke.lua"
            script.write_text("""
local lanes = { 'root-switch', 'composer-root', 'new-comment', 'edited-comment', 'cancel-send', 'send-failure', 'uncertain', 'closed-origin', 'restart', 'batch' }
local results = {}
local output = vim.env.HERDR_TEST_FIXTURE_ROOT .. '/headless-smoke.json'
local function persist() vim.fn.writefile({ vim.json.encode(results) }, output) end
for _, lane in ipairs(lanes) do
  local ok, result = xpcall(function()
    _G.PR01Native.begin(lane)
    if _G.PR01Native.status().composer then _G.PR01Native.headless_save() end
    if lane == 'restart' then
      _G.PR01Native.prepare_restart()
      package.loaded.herdr_review = nil
      require('herdr_review').setup()
      return _G.PR01Native.after_restart()
    end
    return _G.PR01Native.complete(lane)
  end, debug.traceback)
  if ok then
    results[lane] = result
  else
    results[lane] = { lane = lane, status = 'FAIL', error = string.sub(result, 1, 12000),
                      state = _G.PR01Native.status() }
  end
  persist()
  if not ok or results[lane].status ~= 'PASS' then error(lane .. ' failed') end
end
""")
            command = [nvim, "--headless", "-u", str(init), "-i", "NONE", "-l", str(script)]
            completed = None
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=True,
                                           env=isolated_nvim_environment(runtime))
                lanes = json.loads((runtime / "headless-smoke.json").read_text())
            except Exception as error:
                lanes_path = runtime / "headless-smoke.json"
                lanes = read_headless_lanes(lanes_path) if lanes_path.exists() else None
                write_headless_report(artifact, command=command, completed=completed, error=error, lanes=lanes)
                raise
            write_headless_report(artifact, command=command, completed=completed, lanes=lanes)
            return lanes


def scenario_state(nvim, socket):
    return remote(nvim, socket, "vim.json.encode(_G.PR01Native.status())")


def composer_observation(nvim, socket):
    return remote(nvim, socket, "vim.json.encode({window=vim.api.nvim_get_current_win(),buffer=vim.api.nvim_get_current_buf(),filetype=vim.bo.filetype,text=table.concat(vim.api.nvim_buf_get_lines(0, 0, -1, false), '\\n'),mode=vim.api.nvim_get_mode().mode,cursor=vim.api.nvim_win_get_cursor(0)})")


def write_composer_diagnostic(video, name, diagnostic):
    if hasattr(video, "review"):
        write_json(video.review / (name + ".native-composer.json"), diagnostic)


def run_scenario(nvim, socket, name, fixture, video, *, restart=None):
    remote(nvim, socket, "vim.json.encode(_G.PR01Native.begin(" + json.dumps(name) + "))")
    video.capture_for(name + "-opened")
    state = scenario_state(nvim, socket)
    if state.get("composer"):
        diagnostic = {"fixture_state": state}
        try:
            diagnostic["before_input"] = composer_observation(nvim, socket)
            if (not state.get("composer_owned") or not state.get("composer_window")
                    or diagnostic["before_input"].get("window") != state["composer_window"]
                    or diagnostic["before_input"].get("filetype") != "markdown"):
                raise OwnershipError(name + " composer is not the owned fixture composer")
            fixture.key("escape")
            diagnostic["after_escape"] = wait_for(
                lambda: composer_observation(nvim, socket),
                lambda value: value.get("window") == state["composer_window"] and value.get("filetype") == "markdown"
                and value.get("mode") == "n", name + " composer escape")
            fixture.key("text", "ggVGc")
            diagnostic["after_clear"] = wait_for(
                lambda: composer_observation(nvim, socket),
                lambda value: value.get("window") == state["composer_window"] and value.get("filetype") == "markdown"
                and value.get("mode", "").startswith("i") and value.get("text") == "", name + " composer clear")
            fixture.key("text", state["composer_text"])
            diagnostic["after_type"] = wait_for(
                lambda: composer_observation(nvim, socket),
                lambda value: value.get("window") == state["composer_window"] and value.get("filetype") == "markdown"
                and value.get("mode", "").startswith("i") and value.get("text") == state["composer_text"],
                name + " composer text")
            fixture.key(state["save_key"])
            diagnostic["fixture_save"] = wait_for(lambda: scenario_state(nvim, socket),
                                                   lambda value: value.get("save_complete"), name + " composer save")
            diagnostic["after_save"] = wait_for(
                lambda: composer_observation(nvim, socket),
                lambda value: value.get("filetype") != "markdown" and value.get("mode") == "n",
                name + " native save completion")
        except Exception as error:
            diagnostic["error"] = str(error)
            write_composer_diagnostic(video, name, diagnostic)
            raise
        write_composer_diagnostic(video, name, diagnostic)
        video.capture(name + "-saved")
    if name == "restart":
        if restart is None:
            raise OwnershipError("restart lane needs the owned Neovim restart callback")
        remote(nvim, socket, "vim.json.encode(_G.PR01Native.prepare_restart())")
        result = restart()
    else:
        remote(nvim, socket, "vim.json.encode(_G.PR01Native.complete(" + json.dumps(name) + "))")
        result = wait_for(lambda: scenario_state(nvim, socket),
                          lambda value: value.get("lane") == name and value.get("complete"), name + " completion")
    video.capture(name + "-complete")
    if result.get("status") != "PASS":
        raise OwnershipError(name + " failed: " + json.dumps(result, sort_keys=True))
    screenshot = dict(LANES)[name]
    receipt = fixture.capture(screenshot, viewport_grid=video.viewport_grid)
    result["screenshot"] = {"status": "PASS", "receipt": receipt, "path": screenshot}
    return result


def launch_nvim(fixture, nvim, socket, init, native_file):
    fixture.run(shell_command(native_nvim_command(nvim, socket, init, native_file)))
    return wait_for(lambda: remote(nvim, socket, "vim.json.encode(_G.PR01Native.ready())"),
                    lambda value: value.get("ready") is True, "PR01 Neovim readiness")


def stop_nvim(nvim, socket):
    subprocess.run([nvim, "--server", str(socket), "--remote-send", "<Esc>:qa!<CR>"],
                   capture_output=True, text=True, timeout=4, check=True)
    wait_for(lambda: Path(socket).exists(), lambda exists: not exists, "owned Neovim did not stop")


def baseline_root_switch(repo, baseline_revision, fixture_revision, fixture, nvim):
    """Run the defect arm against the archived baseline before candidate launch."""
    with source_archive(repo, baseline_revision) as (source, archive):
        socket = nvim_socket_path(fixture.fixture_root)
        scenario, digest = scenario_fixture(repo, fixture_revision, fixture.fixture_root)
        init = init_file(fixture.fixture_root, source, scenario, socket)
        native_file = fixture.fixture_root / "baseline-review.lua"
        native_file.write_text("local baseline = true\n")
        launch_nvim(fixture, nvim, socket, init, native_file)
        remote(nvim, socket, "vim.json.encode(_G.PR01Native.begin('root-switch'))")
        remote(nvim, socket, "vim.json.encode(_G.PR01Native.complete('root-switch'))")
        observed = wait_for(lambda: scenario_state(nvim, socket),
                            lambda value: value.get("lane") == "root-switch" and value.get("complete"),
                            "baseline root-switch completion")
        stop_nvim(nvim, socket)
        return {"status": "DEFECT_REPRODUCED" if observed.get("defect") == "root-b-deleted" else "FAIL",
                "observed": observed, "source_archive": archive, "scenario_fixture_sha256": digest}


def run_review(repo, revision, target_file, artifact_dir, *, baseline=BASELINE, lanes=LANES,
               fixture_factory=WorkspaceFixture):
    artifact = new_artifact(artifact_dir)
    review = artifact / "review"
    lane_names = [name for name, _ in lanes]
    full_review = lane_names == [name for name, _ in LANES]
    result = {"status": "UNVERIFIED", "revision": revision, "baseline": baseline,
              "scope": "full-ten-lane" if full_review else "selected-lanes",
              "selected_lanes": lane_names, "lanes": {}, "review_video": "UNVERIFIED"}
    write_json(artifact / "context.json", {"revision": revision, "baseline": baseline, "target_file": target_file,
                                             "scope": result["scope"], "scenario_names": lane_names})
    preflight = True
    try:
        provenance = checked_source(repo, revision)
        target = load_target(target_file)
        if "capture_calibration" not in target:
            raise OwnershipError("fixture screenshot is UNVERIFIED: private capture calibration is absent")
        nvim = shutil.which("nvim")
        if nvim is None:
            raise OwnershipError("nvim is unavailable")
        result["provenance"] = provenance
        write_json(artifact / "preflight.json", {"status": "PASS", "target": "available", "calibration": "available"})
        preflight = False
        with fixture_factory(review, target) as fixture, source_archive(repo, revision) as (source, archive):
            runtime, socket = fixture.fixture_root, nvim_socket_path(fixture.fixture_root)
            scenario, digest = scenario_fixture(repo, revision, runtime)
            if full_review:
                result["baseline"] = baseline_root_switch(repo, baseline, revision, fixture, nvim)
                if result["baseline"]["status"] != "DEFECT_REPRODUCED":
                    raise OwnershipError("baseline root-switch defect was not reproduced")
            else:
                result["baseline"] = {"status": "UNVERIFIED", "scope": "not-run for selected lanes"}
            init = init_file(runtime, source, scenario, socket)
            native_file = runtime / "review.lua"; native_file.write_text("local value = 'PR01 native review'\n")
            ready = launch_nvim(fixture, nvim, socket, init, native_file)
            grid = fixture.parent_pty_grid()
            write_json(review / "native-ready.json", {"ready": ready, "source_archive": archive,
                                                        "scenario_fixture_sha256": digest, "parent_pty_grid": grid})
            video = ReviewVideo(review, fixture, {"columns": grid["columns"], "rows": grid["rows"]})
            def restart_candidate():
                before = remote(nvim, socket, "vim.json.encode({pid=vim.fn.getpid(),servername=vim.v.servername})")
                stop_nvim(nvim, socket)
                launch_nvim(fixture, nvim, socket, init, native_file)
                after = remote(nvim, socket, "vim.json.encode({pid=vim.fn.getpid(),servername=vim.v.servername})")
                if before.get("pid") == after.get("pid") or after.get("servername") != str(socket):
                    raise OwnershipError("restart did not produce the expected owned Neovim process tuple")
                write_json(review / "restart-process.json", {"before": before, "after": after})
                result = remote(nvim, socket, "vim.json.encode(_G.PR01Native.after_restart())")
                result["process_restart"] = {"before": before, "after": after}
                return result
            for name, _ in lanes:
                result["lanes"][name] = run_scenario(nvim, socket, name, fixture, video,
                                                       restart=restart_candidate)
                write_json(review / (name + ".result.json"), result["lanes"][name])
            if full_review:
                result["review_video"] = video.finish()
                if result["review_video"].get("status") != "PASS":
                    raise OwnershipError("review video is UNVERIFIED: no valid composer interaction excerpt")
            else:
                result["review_video"] = {"status": "UNVERIFIED", "scope": "selected lanes do not establish video acceptance"}
            result["status"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        if result["status"] != "PASS":
            result["status"] = "UNVERIFIED" if preflight and isinstance(error, OwnershipError) else "FAIL"
            if result["status"] == "UNVERIFIED":
                write_json(artifact / "preflight.json", {"status": "UNVERIFIED", "error": str(error)})
    write_json(artifact / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--target-file")
    parser.add_argument("--artifact-dir")
    parser.add_argument("--headless-smoke", action="store_true")
    parser.add_argument("--lanes", help="comma-separated selected native lanes; full review is the default")
    parser.add_argument("--baseline", default=BASELINE)
    args = parser.parse_args()
    repo = HERE.parents[1]
    if args.headless_smoke:
        if args.target_file or not args.artifact_dir or args.lanes:
            parser.error("--headless-smoke requires --artifact-dir and does not accept --target-file")
        run_headless_smoke(repo, args.revision, Path(args.artifact_dir))
        return 0
    if not args.target_file or not args.artifact_dir:
        parser.error("--target-file and --artifact-dir are required for native review")
    requested = [name for name in args.lanes.split(",") if name] if args.lanes else [name for name, _ in LANES]
    known = dict(LANES)
    if not requested or len(set(requested)) != len(requested) or any(name not in known for name in requested):
        parser.error("--lanes must name unique PR01 lanes")
    selected = tuple((name, known[name]) for name in requested)
    return 0 if run_review(repo, args.revision, args.target_file, Path(args.artifact_dir), baseline=args.baseline,
                            lanes=selected)["status"] == "PASS" else 2


if __name__ == "__main__":
    from runtime_lease import runtime_lease_owner
    with runtime_lease_owner(repo=HERE.parents[1]):
        sys.exit(main())
