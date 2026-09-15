#!/usr/bin/env python3
"""Relay one owned viewer PTY into its parent terminal and record rendered bytes."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time


def process_identity(pid):
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "pid=,lstart=,comm="], capture_output=True,
                            text=True, check=True, timeout=3, env={"PATH": os.defpath, "LC_ALL": "C"})
    fields = result.stdout.split(None, 7)
    if len(fields) != 7:
        raise RuntimeError("relay process identity is unavailable")
    return {"pid": int(fields[0]), "start": " ".join(fields[1:6]), "executable": fields[6]}


def execution_observation(pid):
    identity = process_identity(pid)
    raw = identity["executable"]
    observed = {"pid": identity["pid"], "start": identity["start"], "raw_command": raw}
    try:
        buffer = ctypes.create_string_buffer(4096)
        query = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidpath
        query.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32); query.restype = ctypes.c_int
        if query(pid, buffer, len(buffer)) <= 0: raise OSError(ctypes.get_errno(), "proc_pidpath failed")
        value = buffer.value.decode("utf-8", "strict")
        if not Path(value).is_absolute(): raise OSError("proc_pidpath returned a non-absolute path")
        observed["kernel_image_path"] = str(Path(value).resolve(strict=False))
    except (OSError, UnicodeError) as error:
        observed["kernel_image_error"] = str(error)
    return observed


def visible_terminal_text(data):
    """Remove CSI/OSC/control payloads; incomplete escapes are never visible text."""
    text = bytearray(); index = 0
    while index < len(data):
        byte = data[index]
        length = 2 if 0xc2 <= byte <= 0xdf else 3 if 0xe0 <= byte <= 0xef else 4 if 0xf0 <= byte <= 0xf4 else 0
        if length and index + length <= len(data):
            scalar = data[index:index + length]
            try:
                scalar.decode("utf-8", "strict")
            except UnicodeDecodeError:
                pass
            else:
                text.extend(scalar)
                index += length
                continue
        if byte == 0x1b:
            if index + 1 >= len(data): break
            if data[index + 1] == ord('['):
                index += 2
                while index < len(data) and not 0x40 <= data[index] <= 0x7e: index += 1
                index += 1; continue
            if data[index + 1] == ord(']'):
                index += 2
                while index < len(data) and data[index] != 7 and not (data[index:index + 2] == b'\x1b\\'): index += 1
                index += 2 if data[index:index + 2] == b'\x1b\\' else 1; continue
            index += 2; continue
        if byte == 0x9b:
            index += 1
            while index < len(data) and not 0x40 <= data[index] <= 0x7e: index += 1
            index += 1; continue
        if byte == 0x9d:
            index += 1
            while index < len(data) and data[index] not in (7, 0x9c): index += 1
            index += 1; continue
        if 32 <= byte < 127: text.append(byte)
        index += 1
    return text.decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--columns", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.rows <= 0 or args.columns <= 0 or not args.command or args.command[0] != "--":
        raise SystemExit("relay requires positive grid and -- command")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", args.rows, args.columns, 0, 0))
    child = subprocess.Popen(args.command[1:], stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    receipt = {"relay_pid": os.getpid(), "child_pid": child.pid, "grid": {"rows": args.rows, "columns": args.columns},
               "marker": args.marker, "rendered": False}
    def save():
        temporary = args.receipt.with_suffix(args.receipt.suffix + ".tmp")
        temporary.write_text(json.dumps(receipt) + "\n")
        temporary.replace(args.receipt)
    def stop(signum, frame):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop)
    captured = bytearray(); query_tail = b""
    try:
        receipt["child_identity"] = execution_observation(child.pid)
        receipt["relay_identity"] = execution_observation(os.getpid())
        save()
        while child.poll() is None:
            if select.select([master], [], [], .05)[0]:
                data = os.read(master, 65536)
                if not data:
                    break
                captured.extend(data)
                os.write(sys.stdout.fileno(), data)
                query_tail = (query_tail + data)[-8:]
                if b"\x1b[6n" in query_tail:
                    os.write(master, b"\x1b[1;1R")
                if args.marker in visible_terminal_text(bytes(captured)):
                    receipt["rendered"] = True
                    save()
            if receipt["rendered"]:
                time.sleep(.05)
    finally:
        args.output.write_bytes(captured)
        save()
        os.close(master)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=3)
        receipt["child_exit"] = child.returncode
        save()


if __name__ == "__main__":
    main()
