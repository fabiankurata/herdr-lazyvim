#!/usr/bin/env python3
"""Deterministic local LSP used only by disposable editor experiments."""
import json
import os
from pathlib import Path
import sys

Path(sys.argv[1]).write_text(str(os.getpid()))
while True:
    headers = {}
    while line := sys.stdin.buffer.readline():
        if line == b'\r\n':
            break
        key, value = line.decode().split(':', 1)
        headers[key.lower()] = value.strip()
    if not line:
        break
    request = json.loads(sys.stdin.buffer.read(int(headers['content-length'])))
    if request.get('method') == 'exit':
        break
    if 'id' in request:
        result = {'capabilities': {'textDocumentSync': 1}} if request.get('method') == 'initialize' else None
        payload = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}).encode()
        sys.stdout.buffer.write(f'Content-Length: {len(payload)}\r\n\r\n'.encode() + payload)
        sys.stdout.buffer.flush()
