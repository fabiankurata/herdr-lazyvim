#!/usr/bin/env python3
"""Run one save-only native composer fixture in the existing Herdr workspace."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

from workspace_fixture import WorkspaceFixture, load_target, nvim_socket_path, shell_command, write_json


def wait_for(probe, predicate, label, timeout=12):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if predicate(last):
                return last
        except Exception as error:
            last = str(error)
        time.sleep(.1)
    raise RuntimeError(label + "; last observation: " + json.dumps(last))


def archive_source(repo, revision, target):
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", revision, "nvim"],
                             capture_output=True, check=True)
    target.mkdir()
    subprocess.run(["tar", "-xf", "-", "-C", str(target)], input=archive.stdout, check=True)


def keylog_lua(path):
    return (
        "local function hex(value) return (value:gsub('.', function(byte) return string.format('%02x', string.byte(byte)) end)) end\n"
        "vim.on_key(function(key, typed) vim.fn.writefile({vim.json.encode({key_hex=hex(key),typed_hex=hex(typed)})}, "
        + json.dumps(str(path)) + ", 'a') end)\n"
    )


def keylog_records(path):
    records = []
    for row in Path(path).read_text(encoding="ascii").splitlines():
        record = json.loads(row)
        if set(record) != {"key_hex", "typed_hex"} or any(
                not isinstance(value, str) or len(value) % 2 or any(char not in "0123456789abcdef" for char in value)
                for value in record.values()):
            raise RuntimeError("native key log is not ASCII hexadecimal bytes")
        records.append(record)
    if not records:
        raise RuntimeError("native key log is empty")
    return records


def result_exit_code(results):
    required = ("cmd-enter", "ctrl-s", "screenshot")
    return 0 if not results["failures"] and all(results[lane] == "PASS" for lane in required) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--target-file", required=True)
    args = parser.parse_args()
    target = load_target(args.target_file)
    repo = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if args.revision != revision or len(revision) != 40:
        raise ValueError("revision must be the exact checked-out HEAD")
    if subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], text=True):
        raise ValueError("candidate checkout must be clean")
    artifact = Path(args.artifact_dir).resolve()
    if not artifact.is_relative_to(repo / "artifacts"):
        raise ValueError("evidence must stay under this checkout's artifacts directory")
    results = {"cmd-enter": "UNVERIFIED", "ctrl-s": "UNVERIFIED", "screenshot": "UNVERIFIED", "failures": []}
    try:
        with WorkspaceFixture(artifact, target) as fixture:
            runtime = fixture.fixture_root
            source = runtime / "candidate"
            archive_source(repo, revision, source)
            native_file = runtime / "native.lua"
            native_file.write_text('local value = "native synthetic fixture"\n')
            init = runtime / "init.lua"
            socket = nvim_socket_path(runtime)
            keylog = runtime / "nvim-keys.jsonl"
            init.write_text(
                'vim.g.mapleader = " "\n'
                'vim.opt.runtimepath:prepend(' + json.dumps(str(source / "nvim")) + ')\n'
                'vim.env.HERDR_BIN_PATH = "/usr/bin/false"\n'
                'require("herdr_lazyvim").setup()\n'
                + keylog_lua(keylog)
            )
            nvim = shutil.which("nvim")
            if nvim is None:
                raise RuntimeError("nvim is unavailable")
            fixture.run(shell_command([nvim, "--listen", socket, "-u", init, "-i", "NONE", native_file]))

            def remote(lua):
                completed = subprocess.run([nvim, "--server", str(socket), "--remote-expr", "luaeval(" + json.dumps(lua) + ")"],
                                           capture_output=True, text=True, timeout=3, check=True)
                return completed.stdout.strip()

            def diagnostic():
                return json.loads(remote('vim.json.encode({mode=vim.api.nvim_get_mode().mode,file=vim.api.nvim_buf_get_name(0),filetype=vim.bo.filetype,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),cmd=vim.fn.exists(":HerdrReviewComment"),save_cmd=vim.fn.maparg("<D-CR>","i"),save_ctrl=vim.fn.maparg("<C-s>","i"),columns=vim.o.columns,rows=vim.o.lines})'))

            def saved():
                return [json.loads(path.read_text()) for path in (runtime / "state").rglob("herdr-review/*.json")]

            ready = wait_for(diagnostic, lambda value: value["cmd"] == 2 and value["mode"] == "n", "Neovim readiness deadline")
            write_json(artifact / "nvim-ready.json", ready)
            expected = []
            for key, comment in (("cmd-enter", "native cmd enter"), ("ctrl-s", "native ctrl s")):
                fixture.key("text", " rc")
                composer = wait_for(diagnostic, lambda value: value["filetype"] == "markdown" and value["mode"] == "i", "composer did not open")
                if not composer["save_cmd"] or not composer["save_ctrl"]:
                    raise RuntimeError("public composer save mapping is absent")
                fixture.key("text", comment)
                wait_for(diagnostic, lambda value: value["lines"] == [comment], "composer text mismatch")
                fixture.key(key)
                expected.append(comment)
                records = wait_for(saved, lambda rows: len(rows) == 1 and [entry["text"] for entry in rows[0]["comments"]] == expected, "annotation persistence mismatch")
                write_json(artifact / (key + "-persisted.json"), records)
                results[key] = "PASS"
            write_json(artifact / "nvim-keys.json", keylog_records(keylog))
            try:
                grid = diagnostic()
                results["capture"] = fixture.capture(viewport_grid={"columns": grid["columns"], "rows": grid["rows"]})
                results["screenshot"] = "PASS"
            except Exception as error:
                results["failures"].append({"operation": "screenshot", "error": str(error)})
    except Exception as error:
        results["failures"].append({"operation": "native-save", "error": str(error), "traceback": traceback.format_exc()})
    write_json(artifact / "result.json", results)
    print(json.dumps(results, indent=2))
    return result_exit_code(results)


if __name__ == "__main__":
    from runtime_lease import runtime_lease_owner
    with runtime_lease_owner(repo=Path(__file__).resolve().parents[2]):
        sys.exit(main())
