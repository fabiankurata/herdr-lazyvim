#!/usr/bin/env python3
"""Raw PTY byte and resize evidence, separate from native Cmd/screenshots."""
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import tty


def capture(out):
    out.mkdir(parents=True, exist_ok=False)
    master, slave = pty.openpty()
    proc = None
    payload = b"feedback: pty keyboard\n\x00\x1b[31mexact bytes\x04"
    captured = bytearray()
    try:
        tty.setraw(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 31, 97, 0, 0))
        dimensions = struct.unpack("HHHH", fcntl.ioctl(slave, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        proc = subprocess.Popen(["/bin/cat"], stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True)
        os.close(slave)
        slave = None
        os.write(master, payload)
        deadline = time.monotonic() + 2
        while len(captured) < len(payload) and time.monotonic() < deadline:
            if select.select([master], [], [], max(0, deadline - time.monotonic()))[0]:
                try:
                    data = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                captured.extend(data)
        success = bytes(captured) == payload and dimensions == (31, 97)
        (out / "terminal.bin").write_bytes(captured)
        (out / "pty.json").write_text(json.dumps({
            "status": "PASS" if success else "FAIL", "rows": dimensions[0], "cols": dimensions[1],
            "sent_hex": payload.hex(), "received_hex": captured.hex(),
            "native_cmd_keys": "UNVERIFIED", "screenshots": "UNVERIFIED",
        }, indent=2) + "\n")
        if not success:
            raise RuntimeError("PTY byte or dimension mismatch")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        os.close(master)
        if slave is not None:
            os.close(slave)


if __name__ == "__main__":
    capture(Path(sys.argv[1]))
