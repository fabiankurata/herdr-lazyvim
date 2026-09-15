#!/usr/bin/env python3
"""Observe overlapping IDs and a queued late request on owned named servers."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--runner-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'live')
    args = parser.parse_args()
    sys.path.insert(0, str(args.runner_dir.resolve()))
    from owned_session import OwnedSession
    out = args.artifact_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = args.runner_dir.resolve() / 'owned_session.py'
    result = {'owned_session_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    try:
        with OwnedSession(out) as first, OwnedSession(out) as second:
            def call(server, *parts):
                return json.loads(server.run(*parts).stdout)['result']
            a = call(first, 'workspace', 'create', '--cwd', str(first.root), '--label', 'Fixture alpha')
            b = call(second, 'workspace', 'create', '--cwd', str(second.root), '--label', 'Fixture beta')
            assert a['workspace']['workspace_id'] == b['workspace']['workspace_id']
            assert a['root_pane']['pane_id'] == b['root_pane']['pane_id']
            assert first.socket != second.socket
            result['namespace'] = {'status': 'PASS', 'first_socket': str(first.socket), 'second_socket': str(second.socket),
                                   'workspace_id': a['workspace']['workspace_id'], 'pane_id': a['root_pane']['pane_id']}
            pane = call(first, 'pane', 'split', a['root_pane']['pane_id'], '--direction', 'right', '--no-focus')['pane']['pane_id']
            request = {'id': 'pr00-delayed-move', 'method': 'pane.move',
                       'params': {'pane_id': pane, 'destination': {'type': 'new_tab', 'workspace_id': a['workspace']['workspace_id'], 'label': 'PR00 delayed'}, 'focus': False}}
            caller, queue = socket.socketpair()
            release = threading.Event()
            delivered = {}

            def forward():
                try:
                    payload = queue.recv(65536)
                    if not release.wait(3):
                        raise TimeoutError('test did not release queued request')
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.settimeout(3)
                        connection.connect(str(first.socket))
                        delivered['server_send_ns'] = time.monotonic_ns()
                        connection.sendall(payload)
                        data = b''
                        while b'\n' not in data:
                            chunk = connection.recv(65536)
                            if not chunk:
                                raise EOFError('server closed before response')
                            data += chunk
                        delivered['response'] = json.loads(data.split(b'\n', 1)[0])
                except Exception as exc:
                    delivered['error'] = repr(exc)
                finally:
                    queue.close()
            worker = threading.Thread(target=forward)
            worker.start()
            try:
                caller.settimeout(.02)
                caller.sendall(json.dumps(request).encode() + b'\n')
                try:
                    caller.recv(1)
                    raise AssertionError('queued request unexpectedly acknowledged')
                except TimeoutError:
                    timeout_ns = time.monotonic_ns()
            finally:
                caller.close()
                release.set()
                worker.join(timeout=5)
            assert not worker.is_alive()
            assert 'error' not in delivered, delivered
            assert delivered['server_send_ns'] > timeout_ns
            assert delivered['response']['result']['move_result']['changed'] is True, delivered
            moved = call(first, 'pane', 'get', pane)
            untouched = call(second, 'pane', 'list', '--workspace', b['workspace']['workspace_id'])
            assert len(untouched['panes']) == 1
            result['queued_late_effect'] = {'status': 'PASS', 'client_timeout_ns': timeout_ns, **delivered,
                                            'observed_pane': moved,
                                            'scope': 'A local transport queue forwards after the caller times out. This does not test Herdr internal request scheduling or a controller recovery executor.'}
            result['second_server_after'] = untouched
            result['restart_adoption'] = 'UNVERIFIED'
            result['status'] = 'PASS'
    except Exception as exc:
        result.update(status='FAIL', error=repr(exc))
        raise
    finally:
        (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'status': 'PASS', 'scope': 'two live namespaces and queued late real mutation'}))


if __name__ == '__main__':
    main()
