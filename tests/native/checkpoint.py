"""Display an owned isolated scenario inside one approved native fixture pane."""
import json
import os
from pathlib import Path
import ctypes
import shutil
import signal
import subprocess
import sys
import time
import re

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from workspace_fixture import (OwnershipError, WorkspaceFixture, load_target,
                               process_identity, shell_command, write_json)


def checkpoint_context(session, context):
    required = {"viewer_kind", "native_target_file", "isolated_session", "isolated_socket",
                "workspace_id", "tab_id", "editor_pane", "render_marker", "viewport_before", "restore_viewport"}
    if not isinstance(context, dict) or not required <= set(context):
        raise OwnershipError("native checkpoint context is incomplete")
    if context["viewer_kind"] != "herdr":
        raise OwnershipError("native checkpoint viewer kind is invalid")
    if context["isolated_session"] != session.session or context["isolated_socket"] != str(session.socket):
        raise OwnershipError("native checkpoint displayed connection is not the owned isolated session")
    if not isinstance(context["native_target_file"], str) or not Path(context["native_target_file"]).is_absolute():
        raise OwnershipError("native checkpoint target file is invalid")
    if any(not isinstance(context[key], str) or not context[key]
           for key in ("workspace_id", "tab_id", "editor_pane", "render_marker")):
        raise OwnershipError("native checkpoint displayed target is incomplete")
    if not re.fullmatch(r"PR00CAP-[0-9A-F]{12}", context["render_marker"]):
        raise OwnershipError("native checkpoint render marker is invalid")
    if not callable(context.get("retention")):
        raise OwnershipError("native checkpoint retention observer is unavailable")
    if not isinstance(context.get("viewport_before"), dict) or not callable(context.get("restore_viewport")):
        raise OwnershipError("native checkpoint viewport restoration is unavailable")
    return context


def viewer_argv(session, context):
    required = ("HOME", "PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
    if any(key not in session.env or not session.env[key] for key in required):
        raise OwnershipError("native checkpoint isolated viewer environment is incomplete")
    environment = {key: session.env[key] for key in required}
    environment["HERDR_SOCKET_PATH"] = str(session.socket)
    environment["TERM"] = "xterm-256color"
    return ["/usr/bin/env", "-i", *(key + "=" + value for key, value in sorted(environment.items())),
            session.herdr, "--session", session.session], session.herdr, environment


def checkpoint_name(name):
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise OwnershipError("native checkpoint name is unsafe")
    return name


def canonical_executable(value):
    """Resolve an observed executable path without discarding its raw value."""
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise OwnershipError("native checkpoint executable observation is invalid")
    return str(Path(value).resolve(strict=False))


def kernel_image_path(pid):
    """Read the macOS kernel executable path; ps comm is not image authority."""
    buffer = ctypes.create_string_buffer(4096)
    proc_pidpath = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidpath
    proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    proc_pidpath.restype = ctypes.c_int
    length = proc_pidpath(pid, buffer, len(buffer))
    if length <= 0:
        raise OSError(ctypes.get_errno(), "proc_pidpath failed")
    value = buffer.value.decode("utf-8", "strict")
    if not Path(value).is_absolute():
        raise OSError("proc_pidpath returned a non-absolute path")
    return canonical_executable(value)


def execution_observation(identity):
    """Keep the PID/start ownership tuple distinct from executable diagnostics."""
    if not isinstance(identity, dict) or not isinstance(identity.get("pid"), int) or identity["pid"] <= 0:
        raise OwnershipError("native checkpoint process identity is unavailable")
    if not isinstance(identity.get("start"), str) or not identity["start"]:
        raise OwnershipError("native checkpoint process start identity is unavailable")
    raw = identity.get("raw_command", identity.get("raw_executable", identity.get("executable")))
    observed = {"pid": identity["pid"], "start": identity["start"], "raw_command": raw}
    if not isinstance(raw, str) or not raw:
        observed["raw_command_error"] = "ps comm is unavailable"
    try:
        observed["kernel_image_path"] = identity.get("kernel_image_path") or kernel_image_path(identity["pid"])
    except (OSError, UnicodeError) as error:
        observed["kernel_image_error"] = str(error)
    try:
        after = process_identity(identity["pid"])
        if after is None or (after["pid"], after["start"]) != (identity["pid"], identity["start"]):
            observed["kernel_image_error"] = "PID/start changed during kernel image observation"
    except subprocess.CalledProcessError:
        observed["kernel_image_error"] = "process disappeared during kernel image observation"
    return observed


def cleanup_identity(observation):
    """Return the latest verified PID/start identity for safe process cleanup."""
    return {"pid": observation["pid"], "start": observation["start"]}


def wait_for(predicate, label, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise OwnershipError(label)


def scenario_view(snapshot):
    return {key: snapshot.get(key) for key in ("focused_workspace_id", "focused_tab_id", "focused_pane_id", "layouts")}


def stop_viewer(pid, identity):
    if not isinstance(pid, int) or pid <= 0 or identity is None:
        raise OwnershipError("native checkpoint viewer identity is unavailable; resource retained")
    try:
        current = process_identity(pid)
    except subprocess.CalledProcessError:
        return
    if current is None or current.get("pid") != identity.get("pid") or current.get("start") != identity.get("start"):
        raise OwnershipError("native checkpoint viewer identity changed before cleanup")
    os.kill(pid, signal.SIGTERM)
    def disappeared():
        try:
            return process_identity(pid) is None
        except subprocess.CalledProcessError:
            return True
    wait_for(disappeared, "native checkpoint viewer did not stop")


def capture_checkpoint(session, artifact_dir, name, context):
    """Capture one isolated scenario through a disposable approved-workspace parent."""
    context = checkpoint_context(session, context)
    checkpoint = Path(artifact_dir) / checkpoint_name(name)
    command, executable, environment = viewer_argv(session, context)
    expected_executable = {"raw_executable": executable,
                           "canonical_executable": canonical_executable(executable)}
    context_receipt = checkpoint.parent / (checkpoint.name + ".context.json")
    context_receipt.parent.mkdir(parents=True, exist_ok=True)
    write_json(context_receipt, {
        "requested": {key: context[key] for key in ("viewer_kind", "isolated_session", "isolated_socket",
                                                       "workspace_id", "tab_id", "editor_pane", "render_marker",
                                                       "viewport_before", "native_target_file")},
        "expected_viewer_executable": expected_executable,
        "viewer_environment": environment,
    })
    target = load_target(context["native_target_file"])
    viewer_pid = viewer_identity = relay_pid = relay_identity = prior_tab = relay_output = transcript = relay_receipt = original_label = None
    primary_error = cleanup_error = None
    receipt = {"viewer_kind": context["viewer_kind"],
               "parent_connection": {"socket": target["socket"], "workspace_id": target["workspace_id"]},
               "displayed_connection": {"session": session.session, "socket": str(session.socket)}}
    try:
      with WorkspaceFixture(checkpoint, target) as fixture:
        before_snapshot = json.loads(session.run("api", "snapshot").stdout)["result"]["snapshot"]
        try:
            tabs = {tab.get("tab_id"): tab for tab in before_snapshot.get("tabs", [])}
            panes = {pane.get("pane_id"): pane for pane in before_snapshot.get("panes", [])}
            editor = panes.get(context["editor_pane"], {})
            if (tabs.get(context["tab_id"], {}).get("workspace_id") != context["workspace_id"]
                    or tabs.get(editor.get("tab_id"), {}).get("workspace_id") != context["workspace_id"]):
                raise OwnershipError("native checkpoint requested display target changed")
            prior_tab = before_snapshot.get("focused_tab_id")
            session.run("tab", "focus", context["tab_id"])
            original_label = tabs[context["tab_id"]].get("label")
            if not isinstance(original_label, str):
                raise OwnershipError("native checkpoint requested tab label is unavailable")
            session.run("tab", "rename", context["tab_id"], context["render_marker"])
            marker_tab = json.loads(session.run("api", "snapshot").stdout)["result"]["snapshot"]
            if {row.get("tab_id"): row.get("label") for row in marker_tab.get("tabs", [])}.get(context["tab_id"]) != context["render_marker"]:
                raise OwnershipError("native checkpoint render marker was not applied")
            receipt["render_marker"] = {"value": context["render_marker"], "original_label": original_label}
            receipt["viewport_before"] = context["viewport_before"]
            receipt["viewer_execution"] = {"expected": expected_executable}
            write_json(checkpoint / "checkpoint-start.json", {"viewport_before": context["viewport_before"],
                                                               "viewer_execution": receipt["viewer_execution"]})
            preflight = subprocess.run([*command, "api", "snapshot"], capture_output=True, text=True, timeout=3, check=True)
            displayed = json.loads(preflight.stdout)["result"]["snapshot"]
            relay_receipt = fixture.fixture_root / "viewer-relay.json"
            relay_output = fixture.fixture_root / "viewer-render.bin"
            transcript = checkpoint / "viewer-render.bin"
            parent = fixture.parent_pty_grid()
            relay_script = fixture.fixture_root / "viewer_relay.py"
            shutil.copyfile(HERE / "viewer_relay.py", relay_script)
            relay_script.chmod(0o700)
            relay = [sys.executable, str(relay_script), "--receipt", str(relay_receipt), "--output", str(relay_output),
                     "--marker", context["render_marker"], "--rows", str(parent["rows"]), "--columns", str(parent["columns"]), "--", *command]
            # This is input to the fixture's existing shell.  `exec` would replace
            # that owned parent and make its lifetime depend on the short-lived relay.
            fixture.run(shell_command(relay))
            early = json.loads(wait_for(lambda: relay_receipt.read_text() if relay_receipt.exists() else None,
                                        "native checkpoint relay did not publish ownership"))
            early_viewer = execution_observation(early.get("child_identity"))
            early_relay = execution_observation(early.get("relay_identity"))
            viewer_pid, relay_pid = early_viewer["pid"], early_relay["pid"]
            viewer_identity = cleanup_identity(early_viewer)
            relay_identity = cleanup_identity(early_relay)
            receipt["viewer_execution"]["early"] = early_viewer
            receipt["relay_ownership"] = early_relay
            receipt["relay_script"] = str(relay_script)
            write_json(checkpoint / "viewer-early.json", {"viewer_execution": receipt["viewer_execution"],
                                                            "relay_ownership": early_relay})
            def rendered_or_exited():
                observed = json.loads(relay_receipt.read_text())
                return observed if observed.get("rendered") or "child_exit" in observed else None
            rendered = wait_for(rendered_or_exited, "native checkpoint viewer did not render target text")
            if not rendered.get("rendered"):
                raise OwnershipError("native checkpoint viewer did not render target text")
            post_render = execution_observation(wait_for(lambda: process_identity(viewer_pid),
                                                          "native checkpoint viewer disappeared after render"))
            receipt["viewer_execution"]["post_render"] = post_render
            write_json(checkpoint / "viewer-post-render.json", receipt["viewer_execution"])
            if (post_render["pid"], post_render["start"]) != (early_viewer["pid"], early_viewer["start"]):
                raise OwnershipError("native checkpoint viewer ownership changed after render")
            if post_render.get("kernel_image_error"):
                raise OwnershipError("native checkpoint kernel image observation failed")
            if post_render.get("kernel_image_path") != expected_executable["canonical_executable"]:
                raise OwnershipError("native checkpoint viewer executable changed")
            viewer_identity = cleanup_identity(post_render)
            displayed_panes = {pane.get("pane_id"): pane for pane in displayed.get("panes", [])}
            if (scenario_view(displayed)["focused_tab_id"] != context["tab_id"]
                    or context["editor_pane"] not in displayed_panes):
                raise OwnershipError("native checkpoint viewer displayed another tab")
            receipt.update({"parent_fixture": fixture.fixture, "viewer": viewer_identity, "displayed_target": displayed,
                            "viewer_environment": environment,
                            "parent_viewport": parent, "viewport_before": context["viewport_before"], "render": rendered})
            grid = {key: receipt["parent_viewport"][key] for key in ("columns", "rows")}
            receipt["capture"] = fixture.capture(viewport_grid=grid)
        except BaseException as error:
            primary_error = error
        finally:
            if viewer_pid is not None:
                try:
                    stop_viewer(viewer_pid, viewer_identity)
                    receipt["viewer_cleanup"] = "PASS"
                except BaseException as error:
                    cleanup_error = error
                    receipt["viewer_cleanup"] = "RETAINED: " + str(error)
            if relay_pid is not None:
                try:
                    stop_viewer(relay_pid, relay_identity)
                    receipt["relay_cleanup"] = "PASS"
                except BaseException as error:
                    cleanup_error = cleanup_error or error
                    receipt["relay_cleanup"] = "RETAINED: " + str(error)
            if relay_receipt is not None and relay_receipt.exists():
                shutil.copyfile(relay_receipt, checkpoint / "viewer-relay-final.json")
            if relay_output is not None and relay_output.exists():
                shutil.copyfile(relay_output, transcript)
                receipt["viewer_transcript"] = str(transcript)
            if prior_tab and prior_tab != context["tab_id"]:
                try:
                    session.run("tab", "focus", prior_tab)
                except BaseException as error:
                    cleanup_error = cleanup_error or error
                    receipt["focus_restore"] = "RETAINED: " + str(error)
            if original_label is not None:
                try:
                    session.run("tab", "rename", context["tab_id"], original_label)
                    restored = json.loads(session.run("api", "snapshot").stdout)["result"]["snapshot"]
                    if {row.get("tab_id"): row.get("label") for row in restored.get("tabs", [])}.get(context["tab_id"]) != original_label:
                        raise OwnershipError("native checkpoint render marker was not restored")
                    receipt["render_marker_restore"] = "PASS"
                except BaseException as error:
                    cleanup_error = cleanup_error or error
                    receipt["render_marker_restore"] = "RETAINED: " + str(error)
            try:
                receipt["viewport_restore"] = context["restore_viewport"]()
            except BaseException as error:
                cleanup_error = cleanup_error or error
                receipt["viewport_restore"] = "RETAINED: " + str(error)
        after_snapshot = json.loads(session.run("api", "snapshot").stdout)["result"]["snapshot"]
        if scenario_view(after_snapshot) != scenario_view(before_snapshot):
            cleanup_error = cleanup_error or OwnershipError("native checkpoint changed isolated scenario focus or layout")
        try:
            receipt["scenario_retention"] = context["retention"]()
        except BaseException as error:
            cleanup_error = cleanup_error or error
        receipt["isolated_retention"] = "PASS" if cleanup_error is None else "RETAINED"
        receipt["status"] = "PASS" if primary_error is None and cleanup_error is None else "FAIL"
        if primary_error is not None:
            receipt["primary_error"] = str(primary_error)
        if cleanup_error is not None:
            receipt["cleanup_error"] = str(cleanup_error)
        write_json(checkpoint / "checkpoint.json", receipt)
        if primary_error is not None:
            raise primary_error from cleanup_error
        if cleanup_error is not None:
            raise cleanup_error
    except BaseException:
        if checkpoint.exists() and not (checkpoint / "checkpoint.json").exists():
            receipt.update(status="FAIL", primary_error="checkpoint setup failed")
            write_json(checkpoint / "checkpoint.json", receipt)
        raise
    return receipt
