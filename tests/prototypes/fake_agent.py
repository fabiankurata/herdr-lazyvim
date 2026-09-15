#!/usr/bin/env python3
"""Capture synthetic terminal input verbatim without contacting any model."""
import os
from pathlib import Path
import sys
import tty

tty.setraw(sys.stdin.fileno())
os.write(sys.stdout.fileno(), b'\x1b[?2004lPR00 capture ready\r\n')
with Path(sys.argv[1]).open('wb', buffering=0) as capture:
    while True:
        payload = os.read(sys.stdin.fileno(), 65536)
        if not payload:
            break
        capture.write(payload)
