#!/usr/bin/env python3
"""Exercise real Neovim remote UI on owned PTYs. Does not launch Herdr."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=100)
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error('--cycles must be positive')
    out = args.artifact_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    nvim = shutil.which('nvim')
    if not nvim:
        parser.error('nvim is required')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('HERDR_', 'NVIM_', 'XDG_'))}
    env.update(HOME=str(out / 'home'), XDG_CONFIG_HOME=str(out / 'config'),
               XDG_DATA_HOME=str(out / 'data'), XDG_STATE_HOME=str(out / 'state'),
               XDG_CACHE_HOME=str(out / 'cache'), TERM='xterm-256color')
    for name in ('home', 'config', 'data', 'state', 'cache'):
        (out / name).mkdir(exist_ok=True)
    pid_file = out / 'lsp.pid'
    fixture = out / 'fixture.txt'
    fixture.write_text('saved fixture\n')
    init = out / 'init.lua'
    init.write_text('vim.opt.swapfile = false\nvim.opt.shadafile = "NONE"\n' +
                    'vim.cmd.edit(' + json.dumps(str(fixture)) + ')\n' +
                    'vim.api.nvim_buf_set_lines(0, 0, -1, false, {"unsaved fixture"})\n' +
                    'vim.lsp.start({name="pr00-fixture", cmd={' +
                    ','.join(json.dumps(x) for x in [sys.executable, str(ROOT / 'fake_lsp.py'), str(pid_file)]) +
                    '}, root_dir=' + json.dumps(str(out)) + '})\n')
    traces = []
    active_ui = None
    master = slave = None
    with tempfile.TemporaryDirectory(prefix='pr00-nvim-') as socket_dir, (out / 'server.log').open('wb') as server_log, (out / 'terminal.bin').open('wb') as terminal_log:
        sock = str(Path(socket_dir) / 'editor.sock')
        server = subprocess.Popen([nvim, '--headless', '-u', str(init), '-i', 'NONE', '--listen', sock],
                                  env=env, stdout=server_log, stderr=subprocess.STDOUT)

        def rpc(expression):
            p = subprocess.run([nvim, '--server', sock, '--remote-expr', expression], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, text=True)
            if p.returncode:
                raise RuntimeError(p.stderr)
            return json.loads(p.stdout)

        def snapshot():
            return rpc('json_encode(luaeval("{pid=vim.fn.getpid(), lines=vim.api.nvim_buf_get_lines(0,0,-1,false), dirty=vim.bo.modified, grid={vim.o.lines,vim.o.columns}, uis=vim.api.nvim_list_uis(), lsp=#vim.lsp.get_clients({bufnr=0})}"))')

        def drain():
            if master is None:
                return
            while select.select([master], [], [], 0)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                terminal_log.write(chunk)

        def until(predicate, timeout=3):
            deadline = time.monotonic() + timeout
            last = None
            while time.monotonic() < deadline:
                drain()
                try:
                    last = snapshot()
                    if predicate(last):
                        return last
                except (RuntimeError, json.JSONDecodeError):
                    pass
                time.sleep(.01)
            raise AssertionError(('observation deadline', last))

        try:
            before = until(lambda s: s['lsp'] == 1 and pid_file.exists())
            lsp_pid = int(pid_file.read_text())
            assert before['dirty'] and before['lines'] == ['unsaved fixture']
            for cycle in range(args.cycles):
                started = time.perf_counter()
                master, slave = pty.openpty()
                rows, cols = (24, 80) if cycle % 2 == 0 else (32, 100)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
                active_ui = subprocess.Popen([nvim, '--server', sock, '--remote-ui'], env=env,
                                             stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                attached = until(lambda s: len(s['uis']) == 1 and s['grid'] == [rows, cols])
                if cycle == 0:
                    os.write(master, b'A from PTY\x1c\x0e')
                    attached = until(lambda s: s['lines'] == ['unsaved fixture from PTY'])
                rows, cols = rows + 2, cols + 4
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
                os.kill(active_ui.pid, signal.SIGWINCH)
                resized = until(lambda s: s['grid'] == [rows, cols])
                pty_size = list(struct.unpack('HHHH', fcntl.ioctl(slave, termios.TIOCGWINSZ, b'\0' * 8))[:2])
                os.kill(lsp_pid, 0)
                assert resized['pid'] == before['pid'] and resized['lsp'] == 1 and resized['dirty']
                assert resized['lines'] == ['unsaved fixture from PTY']
                assert resized['grid'] == pty_size
                drain()
                os.write(master, b'\x1c\x0e:detach\r')
                deadline = time.monotonic() + 3
                while active_ui.poll() is None and time.monotonic() < deadline:
                    drain()
                    time.sleep(.01)
                if active_ui.poll() is None:
                    raise AssertionError(('detach deadline', snapshot()))
                active_ui = None
                detached = until(lambda s: len(s['uis']) == 0)
                assert detached['pid'] == before['pid'] and detached['lsp'] == 1 and detached['dirty']
                assert detached['lines'] == ['unsaved fixture from PTY']
                os.close(master)
                os.close(slave)
                master = slave = None
                traces.append({'cycle': cycle, 'editor_pid': before['pid'], 'lsp_pid': lsp_pid,
                               'pty': pty_size, 'grid': resized['grid'], 'dirty': detached['dirty'],
                               'lines': detached['lines'], 'elapsed_ms': (time.perf_counter() - started) * 1000})
            assert fixture.read_text() == 'saved fixture\n'
            result = {'status': 'PASS', 'scope': 'real Neovim remote UI on PTY, plain isolated profile',
                      'cycles': args.cycles, 'before': before, 'trace': traces,
                      'herdr_live_backend': 'UNVERIFIED', 'native_cmd_clipboard_focus': 'UNVERIFIED',
                      'lazyvim': 'UNVERIFIED'}
            (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps({k: v for k, v in result.items() if k not in ('before', 'trace')}))
        except Exception as exc:
            (out / 'result.json').write_text(json.dumps({'status': 'FAIL', 'error': str(exc), 'trace': traces}, indent=2) + '\n')
            raise
        finally:
            if active_ui is not None:
                active_ui.kill()
                active_ui.wait(timeout=3)
            for fd in (master, slave):
                if fd is not None:
                    os.close(fd)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=3)


if __name__ == '__main__':
    main()
