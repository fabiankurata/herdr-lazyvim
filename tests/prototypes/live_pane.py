#!/usr/bin/env python3
"""Move an owned dirty editor through a named Herdr session."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import secrets
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

ROOT = Path(__file__).resolve().parent


def pty_grid(fd):
    """Return a terminal's observed rows and columns as JSON-safe integers."""
    rows, columns = struct.unpack('HHHH', fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8))[:2]
    return [rows, columns]


def render_marker():
    return 'PR00CAP-' + secrets.token_hex(6).upper()


def parse_process_identity(output):
    """Parse `ps -o pid= -o lstart= -o comm=` without accepting partial rows."""
    fields = output.strip().split(None, 6)
    if len(fields) != 7 or not fields[0].isdigit() or not fields[6]:
        raise RuntimeError('owned client process identity is unavailable')
    return {'pid': int(fields[0]), 'start': ' '.join(fields[1:6]), 'executable': fields[6]}


def process_identity(pid):
    try:
        completed = subprocess.run(['ps', '-p', str(pid), '-o', 'pid=', '-o', 'lstart=', '-o', 'comm='],
                                   text=True, capture_output=True, check=True, timeout=3)
    except subprocess.CalledProcessError as error:
        raise RuntimeError('owned client process identity is unavailable') from error
    identity = parse_process_identity(completed.stdout)
    if identity['pid'] != pid:
        raise RuntimeError('owned client process identity changed')
    return identity


def editor_visibility(snapshot, pane_id, display_tab):
    editors = [pane for pane in snapshot.get('panes', []) if pane.get('pane_id') == pane_id]
    if len(editors) != 1:
        return {'state': 'invalid', 'reason': 'editor identity is absent or ambiguous',
                'focused_tab_id': snapshot.get('focused_tab_id'), 'editor_tab_id': None}
    editor = editors[0]
    if editor.get('tab_id') != display_tab:
        state = 'hidden'
    elif snapshot.get('focused_tab_id') == display_tab:
        state = 'visible'
    else:
        state = 'invalid'
    return {
        'state': state, 'reason': None if state != 'invalid' else 'display tab is not focused',
        'focused_tab_id': snapshot.get('focused_tab_id'), 'editor_tab_id': editor.get('tab_id'),
    }


def invoke_native_checkpoint(hook, artifact_dir, name, session, context):
    """Persist the preflight beside its exclusive checkpoint directory before dispatch."""
    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    diagnostic = root / (name + '.preflight.json')
    temporary = diagnostic.with_suffix(diagnostic.suffix + '.tmp')
    temporary.write_text(json.dumps(context['viewport_before'], indent=2) + '\n')
    temporary.replace(diagnostic)
    failure = context['viewport_before'].get('failure')
    if failure:
        raise AssertionError('native checkpoint preflight failed: ' + failure)
    return hook(session, root, name, context)


def settle_visible_geometry(observe, recover=None, *, deadline_seconds=1, clock=time.monotonic, sleep=time.sleep):
    """Observe one visible editor under one deadline, with at most one nudge."""
    started = clock()
    observations = []
    recovery = None
    while clock() - started < deadline_seconds:
        observed = observe()
        observations.append(observed)
        if clock() - started >= deadline_seconds:
            break
        geometry = ('content_grid', 'editor_pty_grid', 'nvim_grid')
        matches = observed['content_grid'] == observed['editor_pty_grid'] == observed['nvim_grid']
        if (matches
                and len(observations) >= 2
                and all(observations[-1][key] == observations[-2][key] for key in geometry)):
            return {'settled': observed, 'observations': observations, 'recovery': recovery}
        if not matches and recover is not None and recovery is None:
            recovery = recover()
            if clock() - started >= deadline_seconds:
                break
        sleep(.01)
    return {'settled': None, 'observations': observations, 'recovery': recovery,
            'failure': 'visible editor geometry did not settle within one second'}


def preflight_viewport(snapshot, pane_id, display_tab, observe, recover=None):
    visibility = editor_visibility(snapshot, pane_id, display_tab)
    if visibility['state'] == 'invalid':
        return {'visibility': visibility, 'settled': None, 'observations': [], 'failure': visibility['reason']}
    if visibility['state'] == 'hidden':
        return {'visibility': visibility, 'settled': None, 'observations': []}
    return {'visibility': visibility, **settle_visible_geometry(observe, recover)}


def write_native_onboarding_config(session, artifact):
    root = Path(session.env['XDG_CONFIG_HOME']).resolve()
    if root.parent != Path(session.root).resolve():
        raise AssertionError('native onboarding config is outside the owned runtime')
    path = root / 'herdr' / 'config.toml'
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        with path.open('x') as stream:
            stream.write('onboarding = false\n')
    except FileExistsError as error:
        raise AssertionError('native onboarding config already exists') from error
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (Path(artifact) / 'native-onboarding-config.json').write_text(json.dumps({'path': str(path), 'sha256': digest}) + '\n')
    return {'path': str(path), 'sha256': digest}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--runner-dir', type=Path, default=ROOT.parent / 'live')
    parser.add_argument('--cycles', type=int, default=100)
    parser.add_argument('--legacy-checkout', '--checkout', dest='legacy_checkout', type=Path)
    parser.add_argument('--revision')
    parser.add_argument('--extra-scenarios', action='store_true')
    parser.add_argument('--resize-recovery', choices=('observe', 'nudge'), default='observe')
    parser.add_argument('--native-capture-hook', type=Path, help='Trusted module exposing capture_checkpoint(session, artifact_dir, name, context)')
    parser.add_argument('--native-target-file', type=Path, help='Mode-0600 approved-workspace target for the native capture hook')
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error('--cycles must be positive')
    if args.native_capture_hook and args.native_target_file is None:
        parser.error('--native-capture-hook requires --native-target-file')
    sys.path.insert(0, str(args.runner_dir.resolve()))
    from owned_session import OwnedSession
    out = args.artifact_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source_paths = [Path(__file__).resolve(), args.runner_dir.resolve() / 'owned_session.py', ROOT / 'fake_lsp.py',
                    ROOT / 'fake_agent.py', ROOT / 'controller.rs', ROOT / 'controller.lua']
    capture_hook = None
    if args.native_capture_hook:
        hook_path = args.native_capture_hook.resolve()
        source_paths.append(hook_path)
        spec = importlib.util.spec_from_file_location('pr00_native_capture', hook_path)
        hook_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = hook_module
        spec.loader.exec_module(hook_module)
        capture_hook = hook_module.capture_checkpoint
    (out / 'source-hashes.json').write_text(json.dumps({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}, indent=2) + '\n')
    traces = []
    cycles = []
    client = None
    master = slave = None
    stop = threading.Event()
    nvim = shutil.which('nvim')
    try:
        with OwnedSession(out) as session:
            version = subprocess.run([session.herdr, '--version'], text=True, capture_output=True, check=True).stdout.strip()
            if args.resize_recovery == 'nudge' and version != 'herdr 0.9.0':
                raise AssertionError('experimental resize nudge is scoped to Herdr 0.9.0')
            (out / 'ownership.json').write_text(json.dumps({'session': session.session, 'socket': str(session.socket), 'root': str(session.root)}, indent=2))

            def api(*parts):
                output = session.run(*parts).stdout
                if parts[:2] in (('pane', 'run'), ('pane', 'send-text')) and not output.strip():
                    traces.append({'command': list(parts), 'response': None})
                    return None
                value = json.loads(output)
                traces.append({'command': list(parts), 'response': value})
                return value['result']

            def checkpoint(name, workspace, tab, editor_pane):
                if capture_hook:
                    client_before = process_identity(client.pid)
                    client_grid = pty_grid(master)

                    def editor_viewport(pane):
                        layout = api('pane', 'layout', '--pane', pane)['layout']
                        rect = next((item['rect'] for item in layout['panes'] if item['pane_id'] == pane), None)
                        if rect is None:
                            raise AssertionError('native checkpoint editor pane is absent from its layout')
                        if layout['zoomed'] and layout['focused_pane_id'] == pane:
                            rect = layout['area']
                        observed = rpc()
                        descriptors = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(observed['pid']), '-d', '0', '-Fn'],
                                                     text=True, capture_output=True, check=True, timeout=3).stdout.splitlines()
                        terminals = [line[1:] for line in descriptors if line.startswith('n/dev/tty')]
                        if len(terminals) != 1:
                            raise AssertionError('native checkpoint editor has no observable PTY')
                        with open(terminals[0], 'rb', buffering=0) as terminal:
                            editor_grid = pty_grid(terminal.fileno())
                        return {
                            'pane_id': pane, 'pid': observed['pid'],
                            'content_grid': [rect['height'] - 2, rect['width'] - 2],
                            'editor_pty_grid': editor_grid, 'nvim_grid': observed['grid'],
                            'dirty': observed['dirty'], 'lsp_clients': observed['lsp'],
                        }

                    def settled_viewport():
                        snapshot = api('api', 'snapshot')['snapshot']
                        def recovery():
                            api('pane', 'resize', '--pane', editor_pane, '--direction', 'left', '--amount', '0.01')
                            api('pane', 'resize', '--pane', editor_pane, '--direction', 'right', '--amount', '0.01')
                            return {'kind': 'left/right resize nudge', 'calls': 2}
                        settled = preflight_viewport(snapshot, editor_pane, tab, lambda: editor_viewport(editor_pane),
                                                     recovery if args.resize_recovery == 'nudge' else None)
                        if settled['visibility']['state'] == 'hidden':
                            editor = rpc()
                            return {**settled,
                                    'hidden_editor': {'pane_id': editor_pane, 'pid': editor['pid'], 'nvim_grid': editor['grid'],
                                                      'dirty': editor['dirty'], 'lsp_clients': editor['lsp']}}
                        return settled

                    baseline = settled_viewport()
                    viewport_before = {'client': client_before, 'client_pty_grid': client_grid, **baseline}

                    def retention():
                        observed = rpc()
                        if (observed['pid'] != before['pid'] or observed['lsp'] != 1 or not observed['dirty']
                                or observed['lines'] != ['unsaved fixture']):
                            raise AssertionError('native checkpoint changed editor retention')
                        for capture in captures.values():
                            if capture['path'].read_bytes() != capture['expected']:
                                raise AssertionError('native checkpoint changed fake-agent input')
                        return {'editor': observed, 'fake_agent_inputs': 'PASS'}

                    def restore_viewport():
                        started = time.monotonic()
                        if process_identity(client_before['pid']) != client_before:
                            raise AssertionError('native checkpoint original client identity changed before restoration')
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', *client_grid, 0, 0))
                        os.kill(client_before['pid'], signal.SIGWINCH)
                        deadline = started + 1
                        last = None
                        if viewport_before['visibility']['state'] == 'hidden':
                            restored_client_grid = pty_grid(master)
                            if restored_client_grid != client_grid:
                                raise AssertionError('native checkpoint original client PTY did not restore')
                            retained = retention()
                            return {'status': 'PASS', 'before': viewport_before,
                                    'after': {'client': process_identity(client_before['pid']),
                                              'client_pty_grid': restored_client_grid, 'visibility': 'hidden'},
                                    'retention': retained, 'elapsed_ms': (time.monotonic() - started) * 1000}
                        if viewport_before['settled'] is None:
                            retention()
                            raise AssertionError('native checkpoint visible editor did not settle before capture')
                        while time.monotonic() < deadline:
                            if process_identity(client_before['pid']) != client_before:
                                raise AssertionError('native checkpoint original client identity changed during restoration')
                            last = editor_viewport(editor_pane)
                            restored_client_grid = pty_grid(master)
                            if (restored_client_grid == client_grid
                                    and last['content_grid'] == viewport_before['settled']['content_grid']
                                    and last['editor_pty_grid'] == viewport_before['settled']['editor_pty_grid']
                                    and last['nvim_grid'] == viewport_before['settled']['nvim_grid']):
                                retained = retention()
                                return {'status': 'PASS', 'before': viewport_before,
                                        'after': {'client': process_identity(client_before['pid']),
                                                  'client_pty_grid': restored_client_grid, 'editor': last},
                                        'retention': retained,
                                        'elapsed_ms': (time.monotonic() - started) * 1000}
                            time.sleep(.01)
                        raise AssertionError('native checkpoint viewport did not converge within one second: ' + json.dumps(last))

                    marker = render_marker()
                    invoke_native_checkpoint(capture_hook, out / 'native', name, session, {
                        'viewer_kind': 'herdr', 'native_target_file': str(args.native_target_file.resolve()),
                        'isolated_session': session.session, 'isolated_socket': str(session.socket),
                        'workspace_id': workspace, 'tab_id': tab, 'editor_pane': editor_pane,
                        'render_marker': marker,
                        'editor_socket': str(editor_socket), 'editor_pid': before['pid'],
                        'retention': retention, 'viewport_before': viewport_before,
                        'restore_viewport': restore_viewport,
                        'timing_scope': 'outside lifecycle timing; parent fixture remains on its approved connection'})

            captures = {}

            def start_capture(pane, label):
                path = session.root / (label + '.bin')
                api('pane', 'run', pane, 'exec ' + shlex.join([sys.executable, str(ROOT / 'fake_agent.py'), str(path)]))
                deadline = time.monotonic() + 3
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                if not path.exists():
                    raise AssertionError('capture terminal did not become ready')
                captures[pane] = {'path': path, 'expected': b'', 'label': label}

            def send_markers(phase):
                for pane, capture in captures.items():
                    marker = (capture['label'] + '-' + phase + '\n').encode()
                    api('pane', 'send-text', pane, marker.decode())
                    capture['expected'] += marker
                    deadline = time.monotonic() + 1
                    while capture['path'].read_bytes() != capture['expected'] and time.monotonic() < deadline:
                        time.sleep(.01)
                    assert capture['path'].read_bytes() == capture['expected'], 'fake-agent input bytes differ'

            def rpc():
                expr = 'json_encode(luaeval("{pid=vim.fn.getpid(),dirty=vim.bo.modified,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),grid={vim.o.lines,vim.o.columns},lsp=#vim.lsp.get_clients({bufnr=0})}"))'
                p = subprocess.run([nvim, '--server', str(editor_socket), '--remote-expr', expr],
                                   env=session.env, text=True, capture_output=True, timeout=3, check=True)
                return json.loads(p.stdout)

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
            client_env = session.env | {'TERM': 'xterm-256color'}
            if capture_hook:
                write_native_onboarding_config(session, out)
            client = subprocess.Popen([session.herdr, '--session', session.session], env=client_env,
                                      stdin=slave, stdout=slave, stderr=slave, start_new_session=True)

            def drain():
                with (out / 'terminal.bin').open('wb') as stream:
                    while not stop.is_set():
                        if select.select([master], [], [], .05)[0]:
                            try:
                                data = os.read(master, 65536)
                            except OSError:
                                return
                            if not data:
                                return
                            stream.write(data)
                            if b'\x1b[6n' in data:
                                os.write(master, b'\x1b[1;1R')
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            workspace = api('workspace', 'create', '--cwd', str(session.root), '--label', 'PR00 fixture')
            workspace_id = workspace['workspace']['workspace_id']
            first_tab = workspace['tab']['tab_id']
            first_agent = workspace['root_pane']['pane_id']
            start_capture(first_agent, 'first-agent')
            nested_agent = api('pane', 'split', first_agent, '--direction', 'down', '--cwd', str(session.root), '--no-focus')['pane']['pane_id']
            start_capture(nested_agent, 'nested-agent')
            second = api('tab', 'create', '--workspace', workspace_id, '--cwd', str(session.root), '--label', 'PR00 second')
            second_tab = second['tab']['tab_id']
            second_agent = second['root_pane']['pane_id']
            start_capture(second_agent, 'second-agent')
            send_markers('before')
            editor = api('pane', 'split', first_agent, '--direction', 'right', '--cwd', str(session.root), '--focus')['pane']['pane_id']
            editor_socket = session.root / 'editor.sock'
            lsp_pid_file = session.root / 'lsp.pid'
            fixture = session.root / 'fixture.txt'
            fixture.write_text('saved fixture\n')
            init = session.root / 'init.lua'
            init.write_text('vim.opt.swapfile=false\nvim.opt.shadafile="NONE"\n' +
                            'vim.cmd.edit(' + json.dumps(str(fixture)) + ')\n' +
                            'vim.api.nvim_buf_set_lines(0,0,-1,false,{"unsaved fixture"})\n' +
                            'vim.lsp.start({name="pr00-fixture",cmd={' +
                            ','.join(json.dumps(x) for x in [sys.executable, str(ROOT / 'fake_lsp.py'), str(lsp_pid_file)]) +
                            '},root_dir=' + json.dumps(str(session.root)) + '})\n')
            cold_start = time.perf_counter()
            api('pane', 'run', editor, 'exec ' + shlex.join([nvim, '-u', str(init), '-i', 'NONE', '--cmd', 'let g:herdr_lazyvim_plugin=1', '--listen', str(editor_socket)]))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    before = rpc()
                    if before['lsp'] == 1 and lsp_pid_file.exists():
                        break
                except (subprocess.SubprocessError, json.JSONDecodeError):
                    pass
                time.sleep(.05)
            else:
                raise AssertionError('editor and fixture LSP did not become ready')
            lsp_pid = int(lsp_pid_file.read_text())
            cold_ready_ms = (time.perf_counter() - cold_start) * 1000
            legacy_head = None
            if args.legacy_checkout:
                checkout = args.legacy_checkout.resolve()
                legacy_head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True, capture_output=True, check=True).stdout.strip()
                if args.revision and args.revision != legacy_head:
                    raise AssertionError('legacy checkout revision does not match requested revision')
                wrapper = session.root / 'herdr-owned'
                call_log = session.root / 'legacy-calls.jsonl'
                call_log.write_text('')
                wrapper.write_text('#!' + sys.executable + '\nimport json, os, sys\n' +
                                   'with open(' + repr(str(call_log)) + ', "a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")\n' +
                                   'os.execv(' + repr(session.herdr) + ', ' + repr([session.herdr, '--session', session.session]) + ' + sys.argv[1:])\n')
                wrapper.chmod(0o700)

                def legacy(action, tab, focused):
                    env = session.env | {'HERDR_BIN_PATH': str(wrapper), 'HERDR_PLUGIN_ROOT': str(checkout),
                                         'HERDR_PLUGIN_CONFIG_DIR': str(session.root / 'plugin-config'),
                                         'HERDR_PLUGIN_CONTEXT_JSON': json.dumps({'workspace_id': workspace_id, 'tab_id': tab, 'focused_pane_id': focused})}
                    call_count_before = len(call_log.read_text().splitlines())
                    start = time.perf_counter()
                    result = subprocess.run(['bash', str(checkout / 'herdr/pane.sh'), action], env=env,
                                            text=True, capture_output=True, timeout=5)
                    measurement = {'elapsed_ms': (time.perf_counter() - start) * 1000,
                                   'external_calls': len(call_log.read_text().splitlines()) - call_count_before}
                    traces.append({'legacy_action': action, **measurement, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
                    result.check_returncode()
                    return measurement
            agent_panes = (first_agent, nested_agent, second_agent)
            before_agents = [api('pane', 'process-info', '--pane', pane) for pane in agent_panes]
            owned_pids = [before['pid'], lsp_pid] + [a['process_info']['shell_pid'] for a in before_agents]

            def resources():
                return subprocess.run(['ps', '-p', ','.join(map(str, owned_pids)), '-o', 'pid=,rss='],
                                      text=True, capture_output=True, check=True).stdout

            resources_before = resources()
            initial_tabs = api('tab', 'list', '--workspace', workspace_id)
            checkpoint('initial-visible', workspace_id, first_tab, editor)
            current_tab = first_tab
            for cycle in range(args.cycles):
                started = time.perf_counter()
                target_tab, target_pane = (second_tab, second_agent) if cycle % 2 == 0 else (first_tab, first_agent)
                if args.legacy_checkout:
                    hide_measurement = legacy('toggle', current_tab, editor)
                    geometry_started = time.monotonic()
                    show_measurement = legacy('open', target_tab, target_pane)
                    current_tab = target_tab
                elif cycle % 2 == 0:
                    api('pane', 'zoom', editor, '--on')
                if not args.legacy_checkout:
                    api('pane', 'zoom', editor, '--off')
                    parked = api('pane', 'move', editor, '--new-tab', '--workspace', workspace_id, '--label', 'PR00 owned parking', '--no-focus')
                    editor = parked['move_result']['pane']['pane_id']
                if cycle == 0 and not args.legacy_checkout:
                    sole = session.run('pane', 'move', editor, '--new-tab', '--workspace', workspace_id, '--label', 'PR00 sole-pane probe', '--no-focus', check=False)
                    traces.append({'scenario': 'sole_pane_move', 'exit_code': sole.returncode, 'stdout': sole.stdout, 'stderr': sole.stderr})
                    if sole.returncode == 0:
                        editor = json.loads(sole.stdout)['result']['move_result']['pane']['pane_id']
                rows, cols = (40, 140) if cycle % 2 == 0 else (48, 160)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
                os.kill(client.pid, signal.SIGWINCH)
                if not args.legacy_checkout:
                    geometry_started = time.monotonic()
                    moved = api('pane', 'move', editor, '--tab', target_tab, '--split', 'right', '--target-pane', target_pane, '--focus')
                    editor = moved['move_result']['pane']['pane_id']
                process = api('pane', 'process-info', '--pane', editor)['process_info']
                editor_pid = before['pid']
                descriptors = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(editor_pid), '-d', '0', '-Fn'],
                                             text=True, capture_output=True, check=True).stdout.splitlines()
                terminals = [line[1:] for line in descriptors if line.startswith('n/dev/tty')]
                if len(terminals) != 1:
                    raise AssertionError('owned editor has no observable PTY')
                tty = terminals[0]
                settle_start = time.perf_counter()
                deadline = geometry_started + 1
                nudged = False
                observations = []
                while True:
                    layout = api('pane', 'layout', '--pane', editor)
                    rect = next(p['rect'] for p in layout['layout']['panes'] if p['pane_id'] == editor)
                    if layout['layout']['zoomed'] and layout['layout']['focused_pane_id'] == editor:
                        rect = layout['layout']['area']
                    content = [rect['height'] - 2, rect['width'] - 2]
                    with open(tty, 'rb', buffering=0) as terminal:
                        pty_size = list(struct.unpack('HHHH', fcntl.ioctl(terminal.fileno(), termios.TIOCGWINSZ, b'\0' * 8))[:2])
                    observed = rpc()
                    observations.append({'elapsed_ms': (time.monotonic() - geometry_started) * 1000,
                                         'content': content, 'pty': pty_size, 'grid': observed['grid']})
                    if content == pty_size == observed['grid'] or time.monotonic() >= deadline:
                        break
                    if args.resize_recovery == 'nudge' and not nudged:
                        api('pane', 'resize', '--pane', editor, '--direction', 'left', '--amount', '0.01')
                        api('pane', 'resize', '--pane', editor, '--direction', 'right', '--amount', '0.01')
                        nudged = True
                    time.sleep(.01)
                geometry_elapsed_ms = (time.monotonic() - geometry_started) * 1000
                os.kill(lsp_pid, 0)
                assert observed['pid'] == before['pid'] and observed['lsp'] == 1
                assert observed['dirty'] and observed['lines'] == ['unsaved fixture']
                cycles.append({'cycle': cycle, 'editor_pane': editor, 'editor': observed, 'lsp_pid': lsp_pid,
                               'hide': hide_measurement if args.legacy_checkout else None,
                               'show': show_measurement if args.legacy_checkout else None,
                               'pty': pty_size, 'content': content, 'layout': layout,
                               'geometry_observations': observations, 'resize_nudge': nudged,
                               'geometry_elapsed_ms': geometry_elapsed_ms,
                               'geometry_settle_ms': (time.perf_counter() - settle_start) * 1000,
                               'elapsed_ms': (time.perf_counter() - started) * 1000})
                assert content == pty_size == observed['grid'] and geometry_elapsed_ms <= 1000, 'content/PTY/Neovim geometry did not converge within one overall second'
                if cycle == 0:
                    checkpoint('transfer-geometry', workspace_id, target_tab, editor)
            after_agents = [api('pane', 'process-info', '--pane', pane) for pane in agent_panes]
            assert before_agents == after_agents
            send_markers('after')
            assert fixture.read_text() == 'saved fixture\n'
            extra = {}
            if args.extra_scenarios:
                worker_path = session.root / 'interrupt-worker.py'
                worker_path.write_text('''import json, os, signal, subprocess, sys
from pathlib import Path
planner, herdr, session, pane, workspace, receipt = json.loads(sys.argv[1])
decision = json.loads(subprocess.run(planner + ['toggle','here','idle','verified'], capture_output=True, text=True, check=True).stdout)
assert decision == {'effect':'park','outcome':'hide','retain_guard':True}
Path(receipt + '.intent').write_text(json.dumps({'requested_action':'toggle','progress':'uncertain_hide','decision':decision}))
effect = subprocess.run([herdr,'--session',session,'pane','move',pane,'--new-tab','--workspace',workspace,'--label','PR00 interrupted','--no-focus'],capture_output=True,text=True,check=True)
temporary = Path(receipt + '.tmp')
temporary.write_text(effect.stdout)
temporary.replace(receipt)
signal.pause()
''')
                rust = session.root / 'controller-rust'
                subprocess.run(['rustc', '-O', str(ROOT / 'controller.rs'), '-o', str(rust)], check=True)
                planners = {'rust': [str(rust)], 'lua': [nvim, '--headless', '-u', 'NONE', '-i', 'NONE', '-l', str(ROOT / 'controller.lua')]}
                interrupted = []
                for name, planner in planners.items():
                    # Return to one captured invoking tab before each independent invocation.
                    api('pane', 'zoom', editor, '--off')
                    moved = api('pane', 'move', editor, '--tab', first_tab, '--split', 'right', '--target-pane', first_agent, '--focus')
                    editor = moved['move_result']['pane']['pane_id']
                    receipt = session.root / (name + '-effect.json')
                    worker_args = [planner, session.herdr, session.session, editor, workspace_id, str(receipt)]
                    worker = subprocess.Popen([sys.executable, str(worker_path), json.dumps(worker_args)], env=session.env)
                    try:
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline and not receipt.exists():
                            if worker.poll() is not None:
                                raise AssertionError('interruption driver exited before mutation receipt')
                            time.sleep(.01)
                        if not receipt.exists():
                            raise AssertionError('interruption driver did not reach successful mutation')
                        worker.kill()
                        worker.wait(timeout=3)
                        effect = json.loads(receipt.read_text())
                        assert effect['result']['move_result']['changed']
                        editor = effect['result']['move_result']['pane']['pane_id']
                        observed = rpc()
                        recovery = json.loads(subprocess.run(planner + ['toggle','hidden','uncertain_hide','verified'],
                                                             env=session.env,capture_output=True,text=True,check=True,timeout=5).stdout)
                        assert recovery == {'effect': 'observe', 'outcome': 'uncertain', 'retain_guard': True}
                        assert observed['pid'] == before['pid'] and observed['dirty'] and observed['lines'] == ['unsaved fixture'] and observed['lsp'] == 1
                        assert [api('pane', 'process-info', '--pane', p) for p in agent_panes] == before_agents
                        interrupted.append({'runtime': name, 'driver_exit': worker.returncode, 'intent': json.loads(Path(str(receipt) + '.intent').read_text()),
                                            'successful_effect': effect, 'restarted_planner': recovery, 'editor': observed,
                                            'limitation': 'The test driver executes a pure planner and the Herdr effect, then is killed before acknowledgement. It does not implement production locking or automatic reconciliation.'})
                    finally:
                        if worker.poll() is None:
                            worker.kill()
                            worker.wait(timeout=3)
                    checkpoint('interruption-' + name, workspace_id, first_tab, editor)
                extra['interrupted_sketches'] = interrupted
                sole = api('pane', 'move', editor, '--new-workspace', '--label', 'PR00 sole editor', '--tab-label', 'PR00 editor', '--focus')
                editor = sole['move_result']['pane']['pane_id']
                sole_workspace = sole['move_result']['pane']['workspace_id']
                sole_tab = sole['move_result']['pane']['tab_id']
                sole_before = api('pane', 'list', '--workspace', sole_workspace)
                assert len(sole_before['panes']) == 1
                placeholder = api('pane', 'split', editor, '--direction', 'right', '--cwd', str(session.root), '--no-focus')['pane']['pane_id']
                api('pane', 'run', placeholder, 'exec /bin/cat')
                hidden = api('pane', 'move', editor, '--new-tab', '--workspace', sole_workspace, '--label', 'PR00 owned parking', '--no-focus')
                editor = hidden['move_result']['pane']['pane_id']
                placeholder_identity = api('pane', 'process-info', '--pane', placeholder)
                hidden_tabs = api('tab', 'list', '--workspace', sole_workspace)
                checkpoint('sole-hidden', sole_workspace, sole_tab, editor)
                restored = api('pane', 'move', editor, '--tab', sole_tab, '--split', 'right', '--target-pane', placeholder, '--focus')
                editor = restored['move_result']['pane']['pane_id']
                assert api('pane', 'process-info', '--pane', placeholder) == placeholder_identity
                observed = rpc()
                assert observed['pid'] == before['pid'] and observed['dirty'] and observed['lines'] == ['unsaved fixture'] and observed['lsp'] == 1
                extra['sole_workspace_placeholder'] = {'status': 'PASS', 'before': sole_before, 'hidden_tabs': hidden_tabs,
                                                       'restored_tabs': api('tab', 'list', '--workspace', sole_workspace),
                                                       'placeholder_process': placeholder_identity, 'editor': observed,
                                                       'policy': 'Create one explicitly owned terminal placeholder before hiding the workspace only editor into an explicitly owned parking tab. Keep the placeholder on restore; never close a user terminal.'}
                checkpoint('sole-restored', sole_workspace, sole_tab, editor)
                send_markers('after-interruption')
            result = {'status': 'PASS', 'scope': 'real owned Herdr pane transfers', 'cycles': cycles,
                      'extra_scenarios': extra,
                      'herdr_version': version, 'resize_recovery': args.resize_recovery,
                      'legacy_revision': legacy_head, 'fixture_editor_ready_ms': cold_ready_ms,
                      'resources_before_pid_rss_kib': resources_before, 'resources_after_pid_rss_kib': resources(),
                      'agent_processes_before': before_agents, 'agent_processes_after': after_agents,
                      'fake_agent_inputs': {pane: {'expected_hex': c['expected'].hex(), 'captured_hex': c['path'].read_bytes().hex()} for pane, c in captures.items()},
                      'tabs_before': initial_tabs, 'tabs_after': api('tab', 'list', '--workspace', workspace_id),
                      'geometry_convergence': 'PASS with one-cell borders subtracted from layout rectangles',
                      'sole_pane_hide': 'Recorded in trace; backend policy remains undecided', 'nested_layout': 'PASS',
                      'native_keys': 'UNVERIFIED', 'late_effect': 'UNVERIFIED'}
            (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps({'status': 'PASS', 'cycles': len(cycles), 'scope': result['scope']}))
    except Exception as exc:
        errors = []
        context = exc
        while context is not None:
            errors.append({'type': type(context).__name__, 'message': str(context)})
            context = context.__context__
        (out / 'result.json').write_text(json.dumps({'status': 'FAIL', 'error': str(exc), 'error_chain': errors, 'cycles': cycles}, indent=2) + '\n')
        raise
    finally:
        if client is not None:
            client.kill()
            client.wait(timeout=5)
        stop.set()
        if 'reader' in locals():
            reader.join(timeout=2)
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        (out / 'trace.json').write_text(json.dumps(traces, indent=2) + '\n')


if __name__ == '__main__':
    main()
