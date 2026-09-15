#!/usr/bin/env python3
"""Plain and LazyVim source-flow remote-UI fixtures in the approved pane.

This module is deliberately a source-only lane until its caller releases a GUI run.
It never starts Alacritty; simulated outer fixtures do not claim native coverage.
"""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

HERE = Path(__file__).resolve().parent
LIVE = HERE.parent / "live"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(LIVE) not in sys.path:
    sys.path.append(str(LIVE))

from workspace_fixture import OwnershipError, WorkspaceFixture, load_target, nvim_socket_path, process_identity, shell_command, socket_owner, write_json
from owned_session import OwnedSession
from source import checked_source, new_artifact, source_archive

PROFILE_SPEC = importlib.util.spec_from_file_location("pr00_profile_run", HERE.parent / "profiles" / "run.py")
profile_run = importlib.util.module_from_spec(PROFILE_SPEC)
PROFILE_SPEC.loader.exec_module(profile_run)


DRIVER = r'''#!/usr/bin/env python3
import argparse, errno, fcntl, json, os, pty, select, signal, struct, subprocess, sys, termios, time, tty
from pathlib import Path
def identity(pid):
 out=subprocess.check_output(["/bin/ps","-p",str(pid),"-o","pid=,lstart=,comm="],text=True).split(None,7)
 if len(out)!=7: raise RuntimeError("process identity unavailable")
 return {"pid":int(out[0]),"start":" ".join(out[1:6]),"executable":out[6]}
def save(path,value):
 temp=path.with_suffix(path.suffix+".tmp"); temp.write_text(json.dumps(value)+"\n"); temp.replace(path)
p=argparse.ArgumentParser(); p.add_argument("--receipt",type=Path,required=True); p.add_argument("--transcript",type=Path,required=True); p.add_argument("--recording-path",type=Path,required=True); p.add_argument("--rows",type=int,required=True); p.add_argument("--columns",type=int,required=True); p.add_argument("command",nargs=argparse.REMAINDER); a=p.parse_args()
if not a.command or a.command[0]!="--" or a.rows<=0 or a.columns<=0: raise SystemExit("driver needs grid and command")
master,slave=pty.openpty(); fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack("HHHH",a.rows,a.columns,0,0)); child=subprocess.Popen(a.command[1:],stdin=slave,stdout=slave,stderr=slave,start_new_session=True); os.close(slave)
parent_termios=termios.tcgetattr(sys.stdin.fileno()) if os.isatty(sys.stdin.fileno()) else None
if parent_termios is not None: tty.setraw(sys.stdin.fileno())
receipt={"driver":identity(os.getpid()),"ui":identity(child.pid),"grid":{"rows":a.rows,"columns":a.columns},"rendered":False}; save(a.receipt,receipt)
def stop(*_):
 if child.poll() is None: os.killpg(child.pid,signal.SIGTERM)
signal.signal(signal.SIGTERM,stop); captured=bytearray(); tail=b""
try:
 while child.poll() is None:
  readable,_,_=select.select([master,sys.stdin.fileno()],[],[],.05)
  if sys.stdin.fileno() in readable:
   data=os.read(sys.stdin.fileno(),65536)
   if data: os.write(master,data)
  if master in readable:
   try: data=os.read(master,65536)
   except OSError as error:
    if error.errno==errno.EIO: break
    raise
   if not data: break
   if a.recording_path.read_text() == "on\n": captured.extend(data)
   os.write(sys.stdout.fileno(),data); tail=(tail+data)[-8:]
   if b"\x1b[6n" in tail: os.write(master,b"\x1b[1;1R")
   receipt["rendered"]=True; save(a.receipt,receipt)
finally:
 if parent_termios is not None: termios.tcsetattr(sys.stdin.fileno(),termios.TCSADRAIN,parent_termios)
 a.transcript.write_bytes(captured)
 if child.poll() is None: os.killpg(child.pid,signal.SIGTERM); child.wait(timeout=3)
 receipt["ui_exit"]=child.returncode; save(a.receipt,receipt); os.close(master)
'''


def wait_for(probe, predicate, label, timeout=8):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if predicate(last):
                return last
        except Exception as error:
            last = {"error": str(error)}
        time.sleep(.05)
    raise OwnershipError(label + ": " + json.dumps(last))


def exact_identity(value):
    if not isinstance(value, dict) or not isinstance(value.get("pid"), int) or not value.get("start"):
        raise OwnershipError("owned process identity is unavailable")
    return {key: value[key] for key in ("pid", "start", "executable")}


def stop_owned(identity):
    try:
        current = process_identity(identity["pid"])
    except subprocess.CalledProcessError:
        return
    if current is None:
        return
    if current != identity:
        raise OwnershipError("owned process identity changed; resource retained")
    os.kill(identity["pid"], signal.SIGTERM)
    def gone():
        try:
            return process_identity(identity["pid"]) is None
        except subprocess.CalledProcessError:
            return True
    wait_for(gone, lambda value: value,
             "owned process did not stop")


def remote_expression(nvim, socket, lua, *, env=None):
    command = [nvim, "--server", str(socket), "--remote-expr", "luaeval(" + json.dumps(lua) + ")"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=3, check=True)
    return json.loads(result.stdout)


class ClipboardProtocol:
    """Bounded line-JSON client; receipts never include the prior clipboard."""
    def __init__(self, command, *, timeout=3):
        self.timeout = timeout
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
        self.terminal = None
        try:
            ready = self._read()
            if ready.get("event") != "ready" or ready.get("status") != "PASS":
                raise OwnershipError("clipboard helper is not ready: " + json.dumps(ready))
        except BaseException:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=self.timeout)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            raise

    def _read(self):
        if self.process.stdout is None:
            raise OwnershipError("clipboard helper stdout is unavailable")
        ready, _, _ = select.select([self.process.stdout], [], [], self.timeout)
        if not ready:
            raise OwnershipError("clipboard helper response timed out")
        line = self.process.stdout.readline()
        if not line:
            raise OwnershipError("clipboard helper exited without a receipt")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise OwnershipError("clipboard helper emitted invalid JSON") from error
        if not isinstance(value, dict):
            raise OwnershipError("clipboard helper receipt is invalid")
        return value

    def command(self, value):
        if self.terminal is not None:
            return self.terminal
        if self.process.stdin is None or self.process.poll() is not None:
            raise OwnershipError("clipboard helper is unavailable")
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        receipt = self._read()
        if value.get("op") in ("restore", "terminate"):
            self.terminal = receipt
        return receipt

    def close(self):
        error = None
        try:
            if self.terminal is None:
                self.terminal = self.command({"op": "terminate"})
        except BaseException as caught:
            error = caught
        try:
            self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=self.timeout)
                error = error or OwnershipError("clipboard helper required KILL")
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if error is not None:
            raise error
        return self.terminal


def clipboard_receipt_status(receipt):
    if not isinstance(receipt, dict) or receipt.get("status") not in ("PASS", "FAIL", "UNVERIFIED"):
        raise OwnershipError("clipboard helper receipt has no verdict")
    return receipt["status"]


def clipboard_lua_state():
    return "vim.json.encode({win=vim.api.nvim_get_current_win(),buf=vim.api.nvim_get_current_buf(),cursor=vim.api.nvim_win_get_cursor(0),dirty=vim.bo.modified,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),lsp=#vim.lsp.get_clients({bufnr=0})})"


def lua_function(body):
    return "(function() " + body + " end)()"


def lua_json(value):
    return "vim.json.decode(" + json.dumps(json.dumps(value, separators=(",", ":"))) + ")"


def write_recording_mode(path, enabled):
    Path(path).write_text("on\n" if enabled else "off\n")


def run_clipboard_exercise(source, runtime, fixture, nvim, socket, server_env, *, client_factory=ClipboardProtocol,
                           bridge="NATIVE", receipt=None):
    """Exercise only the explicit native clipboard option after remote-UI readiness."""
    result = receipt if receipt is not None else {}
    result.update(status="FAIL", bridge=bridge, focus="UNVERIFIED", escape="UNVERIFIED")
    helper_source = Path(source) / "tests/native/clipboard.swift"
    helper = Path(runtime) / "clipboard-helper"
    if bridge == "NATIVE":
        if sys.platform != "darwin":
            result.update(status="UNVERIFIED", reason="macos-appkit-unavailable", bridge="UNVERIFIED")
            return result
        swiftc = shutil.which("swiftc", path=server_env.get("PATH"))
        if swiftc is None:
            result.update(status="UNVERIFIED", reason="swiftc-unavailable", bridge="UNVERIFIED")
            return result
        subprocess.run([swiftc, str(helper_source), "-o", str(helper)], check=True,
                       capture_output=True, text=True, timeout=30)
    elif client_factory is ClipboardProtocol:
        raise OwnershipError("simulated clipboard bridge requires an injected fixture client")
    original = remote_expression(nvim, socket, clipboard_lua_state(), env=server_env)
    scratch = remote_expression(nvim, socket,
        lua_function("vim.cmd('belowright new'); vim.bo.buftype='nofile'; vim.bo.bufhidden='wipe'; vim.bo.swapfile=false; vim.api.nvim_buf_set_lines(0,0,-1,false,{}); return vim.json.encode({win=vim.api.nvim_get_current_win(),buf=vim.api.nvim_get_current_buf()})"),
        env=server_env)
    client = None
    try:
        client = client_factory([str(helper), "--general", "--idle-timeout-ms", "60000"])
        seed = "pr00-clipboard-paste-" + uuid.uuid4().hex
        seeded = client.command({"op": "seed", "nonce": seed})
        if clipboard_receipt_status(seeded) != "PASS":
            result.update(status=seeded["status"], reason=seeded.get("reason", "seed-failed"))
            return result
        if bridge == "SIMULATED":
            remote_expression(nvim, socket, lua_function("vim.g.pr00_fake_clipboard=" + json.dumps(seed) + "; return vim.json.encode(true)"), env=server_env)
        scratch_ready = wait_for(lambda: remote_expression(nvim, socket,
                                 "vim.json.encode({mode=vim.api.nvim_get_mode().mode,win=vim.api.nvim_get_current_win(),buf=vim.api.nvim_get_current_buf(),lines=vim.api.nvim_buf_get_lines(0,0,-1,false)})", env=server_env),
                                 lambda value: value == {"mode": "n", "win": scratch["win"], "buf": scratch["buf"], "lines": [""]},
                                 "clipboard scratch readiness")
        fixture.fixture_focus()
        fixture.key("text", '"+p')
        pasted = wait_for(lambda: remote_expression(nvim, socket,
                          "vim.json.encode((function() local lines=vim.api.nvim_buf_get_lines(0,0,-1,false); return #lines==1 and lines[1]==" + json.dumps(seed) + " end)())", env=server_env),
                          lambda value: value is True, "native clipboard paste")
        yank = "pr00-clipboard-yank-" + uuid.uuid4().hex
        remote_expression(nvim, socket, lua_function("vim.api.nvim_buf_set_lines(0,0,-1,false," + lua_json([yank]) + "); return vim.json.encode(true)"), env=server_env)
        wait_for(lambda: remote_expression(nvim, socket, "vim.json.encode(vim.api.nvim_get_mode().mode)", env=server_env),
                 lambda value: value == "n", "clipboard yank normal mode")
        fixture.fixture_focus()
        fixture.key("text", 'gg"+yy')
        if bridge == "NATIVE":
            wait_for(lambda: remote_expression(nvim, socket, "vim.json.encode(vim.fn.getreg('+')==" + json.dumps(yank + "\n") + ")", env=server_env),
                     lambda value: value is True, "native clipboard yank")
        if bridge == "SIMULATED":
            wait_for(lambda: remote_expression(nvim, socket, "vim.json.encode(vim.g.pr00_fake_clipboard==" + json.dumps(yank + "\n") + ")", env=server_env),
                     lambda value: value is True, "simulated clipboard yank")
        checked = client.command({"op": "check", "expected": yank + "\n"})
        if clipboard_receipt_status(checked) != "PASS":
            result.update(status=checked["status"], reason=checked.get("reason", "yank-check-failed"))
            return result
        result.update(status="PASS", focus="PASS", escape="UNEXERCISED: register commands remained in normal mode",
                      scratch=scratch_ready, pasted=pasted, yanked="matched")
        return result
    finally:
        cleanup_error = None
        if client is not None:
            try:
                restoration = client.close()
                status = clipboard_receipt_status(restoration)
                result["restoration"] = status
                if status != "PASS":
                    result["status"] = status
                    result["restoration_reason"] = restoration.get("reason")
            except BaseException as error:
                result["restoration"] = "UNVERIFIED"
                result["restoration_error"] = str(error)
                cleanup_error = error
        try:
            restored = remote_expression(nvim, socket,
                lua_function("local s=" + lua_json(scratch) + "; local o=" + lua_json(original) + "; if vim.api.nvim_win_is_valid(s.win) then vim.api.nvim_set_current_win(s.win); vim.cmd('close!') end; if vim.api.nvim_win_is_valid(o.win) then vim.api.nvim_set_current_win(o.win) end; return " + clipboard_lua_state()), env=server_env)
            if restored != original:
                raise OwnershipError("clipboard scratch cleanup did not retain original buffer, view, or LSP")
        except BaseException as error:
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise cleanup_error


def remote_context(revision, target_file, session):
    if not isinstance(revision, str) or len(revision) != 40:
        raise ValueError("revision must be exact")
    if not isinstance(target_file, str) or not Path(target_file).is_absolute():
        raise OwnershipError("target file is required")
    return {"subject": "plain-remote-ui", "revision": revision, "target_file": target_file,
            "isolated_session": session.session, "isolated_socket": str(session.socket),
            "lazyvim": "PENDING", "clipboard": "PENDING"}


def write_context(artifact, context):
    path = Path(artifact) / "remote-ui.context.json"
    write_json(path, context)
    return path


def ready_state(value, parent):
    return (isinstance(value, dict) and value.get("dirty") is True
            and value.get("lines") == ["remote ui unsaved"] and value.get("lsp") == 1
            and isinstance(value.get("uis"), list) and len(value["uis"]) == 1
            and value.get("grid") == [parent["rows"], parent["columns"]])


def saved_annotations(state_root):
    return [json.loads(path.read_text()) for path in Path(state_root).rglob("herdr-review/*.json")]


def profile_files(runtime, source, profile_name, artifact, *, fake_clipboard=False):
    profile = runtime / profile_name
    profile.mkdir()
    shutil.copytree(source / "nvim", profile / "nvim", symlinks=False)
    file, lsp_pid, keylog = profile / "fixture.lua", profile / "lsp.pid", profile / "keys.jsonl"
    file.write_text('local synthetic = "remote ui fixture"\n')
    fake_lsp = profile / "fake_lsp.py"
    shutil.copyfile(source / "tests/prototypes/fake_lsp.py", fake_lsp)
    site = profile / "site"
    site.mkdir()
    dependencies = {}
    if profile_name == "lazy":
        for name, repo in profile_run.dependency_paths().items():
            if not repo.is_dir():
                raise OwnershipError("LazyVim dependency is UNVERIFIED: " + name)
            revision = profile_run.git(repo, "rev-parse", "HEAD")
            profile_run.exact_revision(repo, revision)
            dependencies[name] = profile_run.archive_copy(repo, revision, site / name)
    init = profile_run.profile_init(SimpleNamespace(root=profile), profile_name, profile / "nvim", site)
    with init.open("a") as stream:
        stream.write("\nvim.g.mapleader=' '; vim.opt.swapfile=false; vim.opt.shadafile='NONE'\n" +
                    "vim.env.HERDR_BIN_PATH='/usr/bin/false'\n" +
                    "vim.cmd.edit(" + json.dumps(str(file)) + ")\n" +
                    "vim.api.nvim_buf_set_lines(0,0,-1,false,{'remote ui unsaved'})\n" +
                    "vim.lsp.start({name='pr00-remote',cmd={" + json.dumps(sys.executable) + "," + json.dumps(str(fake_lsp)) + "," + json.dumps(str(lsp_pid)) + "},root_dir=" + json.dumps(str(profile)) + "})\n" +
                    "vim.on_key(function(k,t) vim.fn.writefile({vim.json.encode({key_hex=(k:gsub('.',function(c)return string.format('%02x',string.byte(c))end)),typed_hex=(t:gsub('.',function(c)return string.format('%02x',string.byte(c))end))})}," + json.dumps(str(keylog)) + ",'a') end)\n")
        if fake_clipboard:
            stream.write("vim.g.clipboard={name='pr00-simulated',copy={['+']=function(lines,regtype) local text=table.concat(lines,'\\n'); if regtype=='V' then text=text:gsub('\\n+$','')..'\\n' end; vim.g.pr00_fake_clipboard=text end},paste={['+']=function() return vim.split(vim.g.pr00_fake_clipboard or '', '\\n',{plain=true,trimempty=true}),'V' end}}\n")
    write_json(artifact / (profile_name + "-dependencies.json"), {"profile": profile_name, "dependencies": dependencies,
               "optional_plugins": "UNVERIFIED"})
    return profile, init, file, lsp_pid, keylog, dependencies


@contextlib.contextmanager
def fixture_cleanup(getter, artifact, receipt):
    """Run owned child cleanup before the fixture removes its private runtime."""
    primary = None
    try:
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        fixture, server, server_identity, server_log, lsp_identity, ui, driver, transcript = getter()
        errors = []
        if ui is not None:
            try:
                stop_owned(ui)
            except BaseException as error:
                errors.append("UI: " + str(error))
        if driver is not None:
            try:
                def driver_running():
                    try:
                        current = process_identity(driver["pid"])
                    except subprocess.CalledProcessError:
                        return False
                    if current == driver:
                        return True
                    if current is not None and current["pid"] == driver["pid"] and current["start"] == driver["start"]:
                        return False
                    raise OwnershipError("owned driver identity changed; resource retained")
                if driver_running():
                    stop_owned(driver)
            except BaseException as error:
                errors.append("driver: " + str(error))
        if server is not None:
            try:
                if server.poll() is None:
                    if server_identity is None:
                        raise OwnershipError("owned server identity is unavailable; runtime retained")
                    try:
                        current = process_identity(server_identity["pid"])
                    except subprocess.CalledProcessError:
                        current = None
                    if current != server_identity:
                        raise OwnershipError("owned server identity changed; resource retained")
                    server.terminate()
                    try:
                        server.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        current = process_identity(server_identity["pid"])
                        if current != server_identity:
                            raise OwnershipError("owned server identity changed after TERM; resource retained")
                        server.kill()
                        server.wait(timeout=3)
                else:
                    server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                errors.append("owned server was not reaped after KILL")
            except BaseException as error:
                errors.append("server: " + str(error))
        if server_log is not None:
            server_log.close()
        if lsp_identity is not None:
            try:
                def lsp_current():
                    try:
                        return process_identity(lsp_identity["pid"])
                    except subprocess.CalledProcessError:
                        return None
                wait_for(lsp_current, lambda value: value is None,
                         "owned LSP remained after server cleanup", timeout=3)
            except OwnershipError as error:
                errors.append(str(error))
        if transcript is not None and transcript.exists():
            shutil.copyfile(transcript, Path(artifact) / "remote-ui-terminal.bin")
        if errors:
            fixture.retain_runtime("remote UI cleanup: " + "; ".join(errors))
            receipt.setdefault("cleanup_errors", []).extend(errors)
            receipt["status"] = "FAIL"
            if primary is None:
                raise OwnershipError("remote UI cleanup failed: " + "; ".join(errors))
        write_json(Path(artifact) / "result.json", receipt)


def run_lane(repo, revision, artifact_dir, target_file, *, profile="plain", clipboard=False,
             clipboard_bridge="NATIVE", session_factory=OwnedSession, fixture_factory=WorkspaceFixture,
             clipboard_client_factory=ClipboardProtocol):
    """Run the released lane; callers must supply a clean exact checkout and private target."""
    provenance = checked_source(repo, revision)
    target = load_target(target_file)
    artifact = new_artifact(artifact_dir)
    if profile not in ("plain", "lazy"):
        raise ValueError("profile must be plain or lazy")
    if clipboard_bridge not in ("NATIVE", "SIMULATED"):
        raise ValueError("clipboard bridge is invalid")
    server = driver = ui = server_identity = lsp_identity = transcript = server_log = None
    receipt = {"status": "FAIL", "provenance": provenance, "profile": profile,
               "lazyvim": {"status": "UNVERIFIED", "scope": "not-run", "native": "UNVERIFIED"},
               "clipboard": "UNVERIFIED"}
    if profile == "lazy":
        missing = [name for name, path in profile_run.dependency_paths().items() if not path.is_dir()]
        if missing:
            receipt.update(status="UNVERIFIED", lazyvim={"status": "UNVERIFIED", "scope": "local-dependency-preflight",
                           "native": "UNVERIFIED", "missing_dependencies": missing})
            write_json(artifact / "result.json", receipt)
            return receipt
    with session_factory(artifact / "isolated", real=False) as session:
        context = remote_context(revision, target_file, session)
        write_context(artifact, context)
        try:
            with fixture_factory(artifact / "fixture", target) as fixture, source_archive(repo, revision) as (source, archive), fixture_cleanup(lambda: (fixture, server, server_identity, server_log, lsp_identity, ui, driver, transcript), artifact, receipt):
                runtime = fixture.fixture_root
                profile_root, init, file, lsp_pid, keylog, dependencies = profile_files(
                    runtime, source, profile, artifact, fake_clipboard=clipboard and clipboard_bridge == "SIMULATED")
                server_env = dict(session.env, HERDR_BIN_PATH="/usr/bin/false")
                for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                    server_env[key] = str(profile_root / key.lower())
                    Path(server_env[key]).mkdir(exist_ok=True)
                nvim = shutil.which("nvim", path=session.env.get("PATH"))
                if nvim is None:
                    raise OwnershipError("nvim is unavailable")
                socket = nvim_socket_path(runtime)
                server_log = (artifact / "server.log").open("wb")
                server = subprocess.Popen([nvim, "--headless", "-u", str(init), "-i", "NONE", "--listen", socket], env=server_env,
                                          stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)
                ready = wait_for(lambda: remote_expression(nvim, socket, "vim.json.encode({dirty=vim.bo.modified,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),uis=vim.api.nvim_list_uis(),grid={vim.o.lines,vim.o.columns},lsp=#vim.lsp.get_clients({bufnr=0}),lazy_imported=vim.g.profile_lazyvim_imported==true,lazy_config=package.loaded['lazyvim.config']~=nil,errors=vim.g.profile_errors or {}})"),
                                 lambda value: value["dirty"] and value["lines"] == ["remote ui unsaved"] and value["lsp"] == 1 and lsp_pid.exists() and not value["errors"] and (profile != "lazy" or value["lazy_imported"] and value["lazy_config"]), profile + " server readiness")
                server_identity = wait_for(lambda: process_identity(server.pid), lambda value: value is not None, "server disappeared")
                lsp_identity = wait_for(lambda: process_identity(int(lsp_pid.read_text())), lambda value: value is not None,
                                        "LSP disappeared before attach")
                owner = socket_owner(Path(socket))
                if owner["owner"] != server_identity:
                    raise OwnershipError("owned server socket identity differs")
                parent = fixture.parent_pty_grid()
                driver_script, driver_receipt, transcript, recording = runtime / "remote_ui_driver.py", runtime / "remote-ui-driver.json", runtime / "remote-ui-terminal.bin", runtime / "remote-ui-recording"
                write_recording_mode(recording, True)
                driver_script.write_text(DRIVER); driver_script.chmod(0o700)
                fixture.run(shell_command([sys.executable, driver_script, "--receipt", driver_receipt, "--transcript", transcript, "--recording-path", recording, "--rows", parent["rows"], "--columns", parent["columns"], "--", nvim, "--server", socket, "--remote-ui"]))
                early = wait_for(lambda: json.loads(driver_receipt.read_text()) if driver_receipt.exists() else None, lambda value: value is not None, "remote UI receipt missing")
                driver, ui = exact_identity(early["driver"]), exact_identity(early["ui"])
                attached = wait_for(lambda: remote_expression(nvim, socket, "vim.json.encode({dirty=vim.bo.modified,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),uis=vim.api.nvim_list_uis(),grid={vim.o.lines,vim.o.columns},lsp=#vim.lsp.get_clients({bufnr=0}),lazy_imported=vim.g.profile_lazyvim_imported==true,lazy_config=package.loaded['lazyvim.config']~=nil,errors=vim.g.profile_errors or {}})"),
                                    lambda value: ready_state(value, parent), "remote UI attach readiness")
                fixture.fixture_focus()
                write_json(artifact / "remote-ui-ready.json", {"context": context, "server": server_identity, "lsp": lsp_identity, "socket": owner, "parent_pty": parent, "driver": driver, "ui": ui, "state": attached, "dependencies": dependencies})
                if clipboard:
                    clipboard_receipt = {"status": "FAIL", "bridge": clipboard_bridge,
                                         "focus": "UNVERIFIED", "escape": "UNVERIFIED"}
                    receipt["clipboard"] = clipboard_receipt
                    write_recording_mode(recording, False)
                    run_clipboard_exercise(source, runtime, fixture, nvim, socket, server_env,
                                           client_factory=clipboard_client_factory, bridge=clipboard_bridge,
                                           receipt=clipboard_receipt)
                    if receipt["clipboard"]["status"] != "PASS":
                        raise OwnershipError("clipboard exercise " + receipt["clipboard"]["status"])
                persisted = []
                composer_lua = "vim.json.encode({mode=vim.api.nvim_get_mode().mode,filetype=vim.bo.filetype,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),cmd=vim.fn.exists(':HerdrReviewComment'),save_cmd=vim.fn.maparg('<D-CR>','i'),save_ctrl=vim.fn.maparg('<C-s>','i')})"
                for kind, text in (("cmd-enter", "remote cmd enter"), ("ctrl-s", "remote ctrl s")):
                    fixture.key("text", " rc")
                    wait_for(lambda: remote_expression(nvim, socket, composer_lua, env=server_env),
                             lambda value: value["mode"] == "i" and value["filetype"] == "markdown" and value["cmd"] == 2
                             and value["save_cmd"] and value["save_ctrl"], "remote composer readiness")
                    fixture.key("text", text)
                    wait_for(lambda: remote_expression(nvim, socket, composer_lua, env=server_env),
                             lambda value: value["lines"] == [text], "remote composer text")
                    fixture.key(kind)
                    persisted.append(text)
                    wait_for(lambda: saved_annotations(server_env["XDG_STATE_HOME"]),
                             lambda rows: len(rows) == 1 and [item["text"] for item in rows[0]["comments"]] == persisted,
                             "synthetic annotation persistence")
                write_json(artifact / "native-key-receipts.json", {"expected": persisted, "saved": saved_annotations(server_env["XDG_STATE_HOME"]),
                                                                       "keys": keylog.read_text(encoding="ascii")})
                fixture.capture(viewport_grid={"columns": parent["columns"], "rows": parent["rows"]})
                stop_owned(ui)
                ui = None
                detached = wait_for(lambda: remote_expression(nvim, socket, "vim.json.encode({pid=vim.fn.getpid(),uis=vim.api.nvim_list_uis(),dirty=vim.bo.modified,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),lsp=#vim.lsp.get_clients({bufnr=0})})"),
                                    lambda value: value["pid"] == server_identity["pid"] and not value["uis"] and value["dirty"] and value["lines"] == ["remote ui unsaved"] and value["lsp"] == 1
                                    and process_identity(lsp_identity["pid"]) == lsp_identity, "remote UI detach retention")
                receipt.update(status="PASS", detached=detached,
                               lazyvim={"status": "PASS" if profile == "lazy" else "UNVERIFIED",
                                        "scope": "archived-source-flow" if profile == "lazy" else "not-run",
                                        "native": "UNVERIFIED",
                                        "outer_fixture": "NATIVE" if isinstance(fixture, WorkspaceFixture) else "SIMULATED"})
        except BaseException as error:
            receipt["primary_error"] = str(error)
            raise
        finally:
            write_json(artifact / "result.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True); parser.add_argument("--artifact-dir", required=True); parser.add_argument("--target-file", required=True); parser.add_argument("--profile", choices=("plain", "lazy"), default="plain"); parser.add_argument("--clipboard", action="store_true", help="run the bounded general-pasteboard experiment")
    args = parser.parse_args(); repo = HERE.parents[1]
    return 0 if run_lane(repo, args.revision, Path(args.artifact_dir), args.target_file, profile=args.profile, clipboard=args.clipboard)["status"] == "PASS" else 2


if __name__ == "__main__":
    from runtime_lease import runtime_lease_owner
    with runtime_lease_owner(repo=HERE.parents[1]):
        sys.exit(main())
