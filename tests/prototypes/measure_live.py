#!/usr/bin/env python3
"""One supervised sample inside a comparator-owned named Herdr runtime."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import threading
import time

ROOT = Path(__file__).resolve().parent
FAILURE_INPUT_LIMIT = 4096
FAILURE_DEFAULT_IDENTITIES_LIMIT = 65536


def unavailable(reason):
    return {'status': 'UNAVAILABLE', 'reason': reason}


def unexercised(reason):
    return {'status': 'UNEXERCISED', 'reason': reason}


def failure_state_receipt(out, *, api, rpc, editor=None, agent=None, capture=None,
                          fixture=None, retention_baseline=None):
    """Collect bounded, read-only state while the owned sample still exists."""
    fields = {}

    def observe(name, callback):
        try:
            fields[name] = {'status': 'OBSERVED', 'value': callback()}
        except Exception as diagnostic_error:
            fields[name] = unavailable(type(diagnostic_error).__name__ + ': ' + str(diagnostic_error)[:512])

    def pane_identity(pane):
        if not isinstance(pane, str):
            raise RuntimeError('pane was not created before failure')
        return api('pane', 'process-info', '--pane', pane)['process_info']

    observe('editor_identity', lambda: pane_identity(editor))
    observe('agent_identity', lambda: pane_identity(agent))
    observe('editor_dirty_text_lsp', rpc)

    def captured_input():
        if capture is None:
            raise RuntimeError('fake input capture was not created before failure')
        size = capture.stat().st_size
        if size > FAILURE_INPUT_LIMIT:
            raise RuntimeError('capture exceeds bounded receipt limit: ' + str(size) + ' bytes')
        value = capture.read_bytes()
        return {'bytes': len(value), 'hex': value.hex(),
                'sha256': hashlib.sha256(value).hexdigest()}

    observe('fake_input', captured_input)

    def saved_fixture():
        if fixture is None:
            raise RuntimeError('saved fixture was not created before failure')
        value = fixture.read_bytes()
        if len(value) > FAILURE_INPUT_LIMIT:
            raise RuntimeError('saved fixture exceeds bounded receipt limit: ' + str(len(value)) + ' bytes')
        return {'bytes': len(value), 'hex': value.hex(),
                'sha256': hashlib.sha256(value).hexdigest()}

    observe('saved_fixture', saved_fixture)

    def default_identities():
        paths = list(out.glob('owned-session-*/default-before.json'))
        if len(paths) != 1:
            raise RuntimeError('expected one parent default-identity receipt')
        if paths[0].stat().st_size > FAILURE_DEFAULT_IDENTITIES_LIMIT:
            raise RuntimeError('parent default-identity receipt exceeds bounded limit')
        return {
            'before': json.loads(paths[0].read_text()),
            'final_comparison_authority': {
                'default_after_path': str(paths[0].with_name('default-after.json')),
                'teardown_path': str(paths[0].with_name('teardown.json')),
            },
        }

    observe('protected_default_before', default_identities)
    return {'status': 'COLLECTED',
            'retention_baseline': retention_baseline or unexercised('retention baselines were not established before failure'),
            'fields': fields}


def retain_failure(result, error, out, **receipt_arguments):
    """The primary measurement error remains authoritative if diagnostics fail."""
    result['error'] = repr(error)
    try:
        result['failure_state'] = failure_state_receipt(out, **receipt_arguments)
    except Exception as diagnostic_error:
        result['failure_state'] = {'status': 'UNAVAILABLE',
                                   'reason': type(diagnostic_error).__name__ + ': ' + str(diagnostic_error)[:512]}
    return result


def measure(request):
    source = Path(request['source'])
    out = Path(request['artifact'])
    runtime = Path(request['runtime_root'])
    session = request['session']
    herdr = request['herdr']
    expected_socket = runtime / 'config/herdr/sessions' / session / 'herdr.sock'
    if not session.startswith('codex-pr00-') or expected_socket != Path(request['socket']) or os.environ.get('HERDR_SOCKET_PATH') != str(expected_socket):
        raise RuntimeError('worker context does not match its supervised named runtime')
    env = dict(os.environ)
    nvim = shutil.which('nvim')
    if not nvim:
        unavailable = {'status': 'UNVERIFIED', 'error': 'Neovim is unavailable', 'metrics': {}}
        (out / 'measurement.json').write_text(json.dumps(unavailable) + '\n')
        return unavailable
    used_source = ['herdr/pane.sh', 'herdr/config.sh', 'herdr/launch.sh',
                   'nvim/lua/herdr_lazyvim.lua', 'nvim/lua/herdr_review.lua', 'nvim/lua/herdr_theme.lua']
    source_hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in used_source}
    mode = request.get('runtime_mode', 'legacy')
    if mode not in ('legacy', 'rust', 'lua'):
        raise ValueError('unknown runtime mode')
    trace = []
    processes = []
    phase = 'setup'
    result = {'status': 'FAIL', 'metrics': {}, 'runtime_source_sha256': source_hashes, 'runtime_mode': mode}
    client = None
    master = slave = None
    stop = threading.Event()

    def run(argv, *, environment=env, timeout=5):
        started = time.monotonic()
        try:
            completed = subprocess.run(argv, env=environment, cwd=runtime, text=True, capture_output=True, timeout=timeout, check=True)
            return completed
        finally:
            processes.append({'phase': phase, 'argv': argv, 'elapsed_ms': (time.monotonic() - started) * 1000})

    def api(*args):
        started = time.perf_counter()
        completed = run([herdr, '--session', session, *args])
        trace.append({'phase': phase, 'api': list(args), 'elapsed_ms': (time.perf_counter() - started) * 1000,
                      'stdout': completed.stdout, 'stderr': completed.stderr})
        if args[:2] in (('pane', 'run'), ('pane', 'send-text')) and not completed.stdout.strip():
            return None
        return json.loads(completed.stdout)['result']

    editor_socket = runtime / 'editor.sock'

    def rpc(expression=None):
        if expression is None:
            lua_table = '{pid=vim.fn.getpid(),dirty=vim.bo.modified,lines=vim.api.nvim_buf_get_lines(0,0,-1,false),grid={vim.o.lines,vim.o.columns},lsp=#vim.lsp.get_clients({bufnr=0}),entered=vim.v.vim_did_enter,marker=vim.g.herdr_lazyvim_plugin,review_command=vim.fn.exists(":HerdrReviewComment"),buffer=vim.api.nvim_buf_get_name(0)}'
            expression = 'json_encode(luaeval(' + json.dumps(lua_table) + '))'
        return json.loads(run([nvim, '--server', str(editor_socket), '--remote-expr', expression], timeout=2).stdout)

    def wait_for(predicate, timeout=5, diagnostics=None):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = rpc()
                if predicate(last):
                    return last
            except (subprocess.SubprocessError, json.JSONDecodeError):
                pass
            time.sleep(.01)
        if diagnostics is not None:
            try:
                result['editor_observation_diagnostic'] = diagnostics(last)
            except Exception as diagnostic_error:
                result['editor_observation_diagnostic'] = {'diagnostic_error': type(diagnostic_error).__name__}
        raise AssertionError(('editor observation deadline', last))

    def terminal_path(pid):
        rows = run(['/usr/sbin/lsof', '-a', '-p', str(pid), '-d', '0', '-Fn']).stdout.splitlines()
        paths = [line[1:] for line in rows if line.startswith('n/dev/tty')]
        if len(paths) != 1:
            raise AssertionError('owned process stdin is not an observable PTY')
        return paths[0]

    def geometry(pane, tty_path, started, *, editor=False, remaining_agent=False):
        deadline = started + 1
        observations = []
        nudged = False
        while True:
            layout = api('pane', 'layout', '--pane', pane)['layout']
            rect = next(item['rect'] for item in layout['panes'] if item['pane_id'] == pane)
            if layout['zoomed'] and layout['focused_pane_id'] == pane:
                rect = layout['area']
            # Herdr's isolated defaults frame splits and reserve a scrollbar
            # column for this normal-screen capture terminal, not the Nvim TUI.
            border = 2 if len(layout['panes']) > 1 else 0
            content = [rect['height'] - border, rect['width'] - border - (0 if editor else 1)]
            with open(tty_path, 'rb', buffering=0) as terminal:
                size = list(struct.unpack('HHHH', fcntl.ioctl(terminal.fileno(), termios.TIOCGWINSZ, b'\0' * 8))[:2])
            observed_grid = rpc()['grid'] if editor else None
            now = time.monotonic()
            observations.append({'elapsed_ms': (now - started) * 1000, 'layout': layout, 'content': content, 'pty': size, 'nvim_grid': observed_grid})
            if content == size and (not editor or observed_grid == size) and now <= deadline:
                return observations
            if now >= deadline:
                result['failed_geometry'] = observations
                raise AssertionError('geometry did not converge within one overall second')
            if (editor or remaining_agent) and mode != 'legacy' and not nudged:
                if version != 'herdr 0.9.0':
                    raise AssertionError('observed resize nudge is only scoped to Herdr 0.9.0')
                recovery_started = time.monotonic()
                if editor:
                    api('pane', 'resize', '--pane', pane, '--direction', 'left', '--amount', '0.01')
                    api('pane', 'resize', '--pane', pane, '--direction', 'right', '--amount', '0.01')
                    kind = 'left/right resize nudge'
                else:
                    api('pane', 'zoom', pane, '--on')
                    api('pane', 'zoom', pane, '--off')
                    kind = 'zoom on/off roundtrip'
                    assert not api('pane', 'layout', '--pane', pane)['layout']['zoomed'], 'agent recovery must finish unzoomed'
                observations[-1]['recovery'] = {'kind': kind, 'calls': 2,
                                                  'duration_ms': (time.monotonic() - recovery_started) * 1000,
                                                  'deadline_ms': (deadline - started) * 1000}
                nudged = True
            time.sleep(.01)

    try:
        version = run([herdr, '--version']).stdout.strip()
        if mode == 'rust':
            rustc = request.get('rustc')
            if not isinstance(rustc, str) or not Path(rustc).is_file():
                raise RuntimeError('outer comparator did not provide a concrete rustc executable')
            binary = runtime / 'controller-rust'
            run([rustc, '-O', str(ROOT / 'controller.rs'), '-o', str(binary)], timeout=20)
            planner = [str(binary)]
            result['prototype_binary_bytes'] = binary.stat().st_size
            result['rustc'] = rustc
        elif mode == 'lua':
            planner = [nvim, '--headless', '-u', 'NONE', '-i', 'NONE', '-l', str(ROOT / 'controller.lua')]
        else:
            planner = None

        def plan(action, observation, expected):
            decision = json.loads(run(planner + [action, observation, 'idle', 'verified']).stdout)
            assert decision == expected, ('unexpected planner decision', decision)
            trace.append({'planner': mode, 'action': action, 'observation': observation, 'decision': decision})

        (out / 'herdr-default-config.toml').write_text(run([herdr, '--default-config']).stdout)
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 140, 0, 0))
        # The outer OwnedSession.execute supervises this worker and its descendants.
        client = subprocess.Popen([herdr, '--session', session], env=env | {'TERM': 'xterm-256color'},
                                  cwd=runtime, stdin=slave, stdout=slave, stderr=slave)

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
        fixture = runtime / 'fixture.txt'
        fixture.write_text('saved fixture\n')
        fixture_initial = fixture.read_bytes()
        result['retention_baseline'] = {
            'editor_before': unexercised('editor state was not observed before failure'),
            'editor_pid': unexercised('editor process was not observed before failure'),
            'agent_before': unexercised('agent state was not observed before failure'),
            'agent_pid': unexercised('agent process was not observed before failure'),
            'lsp_pid': unexercised('LSP process was not observed before failure'),
            'saved_fixture': {'bytes': len(fixture_initial), 'hex': fixture_initial.hex(),
                              'sha256': hashlib.sha256(fixture_initial).hexdigest()},
            'expected_fake_input_prefix': unexercised('no fake-input writes completed before failure'),
        }
        workspace = api('workspace', 'create', '--cwd', str(runtime), '--label', 'PR00 matched performance')
        workspace_id = workspace['workspace']['workspace_id']
        tab = workspace['tab']['tab_id']
        agent = workspace['root_pane']['pane_id']
        capture = runtime / 'agent-input.bin'
        api('pane', 'run', agent, 'exec ' + shlex.join([sys.executable, str(ROOT / 'fake_agent.py'), str(capture)]))
        ready = time.monotonic() + 3
        while not capture.exists() and time.monotonic() < ready:
            time.sleep(.01)
        if not capture.exists():
            raise AssertionError('fake agent did not become ready')
        api('pane', 'send-text', agent, 'fixture-before\n')
        expected_input = b'fixture-before\n'
        result['retention_baseline']['expected_fake_input_prefix'] = {
            'status': 'COMPLETED', 'hex': expected_input.hex(),
            'sha256': hashlib.sha256(expected_input).hexdigest(),
        }
        editor = api('pane', 'split', agent, '--direction', 'right', '--cwd', str(runtime), '--focus')['pane']['pane_id']
        lsp_pid_file = runtime / 'lsp.pid'
        init = runtime / 'fixture-init.lua'
        init.write_text('vim.opt.swapfile=false\nvim.opt.shadafile="NONE"\n' +
                        'vim.api.nvim_create_autocmd("VimEnter", {callback=function() vim.lsp.start({name="pr00-fixture",cmd={' +
                        ','.join(json.dumps(x) for x in [sys.executable, str(ROOT / 'fake_lsp.py'), str(lsp_pid_file)]) +
                        '},root_dir=' + json.dumps(str(runtime)) + '}) end})\n')
        wrapper = runtime / 'nvim-fixture'
        wrapper.write_text('#!' + sys.executable + '\nimport os,sys\nos.execv(' + repr(nvim) + ',' +
                           repr([nvim, '-u', str(init), '-i', 'NONE', '--listen', str(editor_socket)]) + '+sys.argv[1:])\n')
        wrapper.chmod(0o700)
        config = runtime / 'plugin-config'
        config.mkdir()
        (config / 'config.toml').write_text('editor = ' + json.dumps(str(wrapper)) + '\ntarget = ' + json.dumps(str(fixture)) +
                                          '\nreview_enabled = true\nterminal_placement = "zoomed"\nresize_workaround = "nudge"\n')
        launch = ['env', 'HERDR_PLUGIN_ROOT=' + str(source), 'HERDR_PLUGIN_CONFIG_DIR=' + str(config),
                  'bash', str(source / 'herdr/launch.sh')]
        phase = 'cold-ready'
        cold_process_start = len(processes)
        cold_started = time.perf_counter()
        if planner:
            plan('show', 'absent', {'effect': 'spawn', 'outcome': 'show', 'retain_guard': True})
        api('pane', 'run', editor, 'exec ' + shlex.join(launch))
        cold = wait_for(
            lambda state: state['entered'] == 1 and state['marker'] == 1
            and state['review_command'] == 2 and Path(state['buffer']).resolve() == fixture.resolve(),
            diagnostics=lambda last: {
                'last_rpc_state': last,
                'editor_socket': str(editor_socket),
                'editor_socket_exists': editor_socket.exists(),
                'editor_socket_parent_exists': editor_socket.parent.exists(),
                'launch_argv': launch,
                'plugin_config': (config / 'config.toml').read_text(),
                'wrapper': wrapper.read_text(),
                'editor_pane_process_info': api('pane', 'process-info', '--pane', editor)['process_info'],
            },
        )
        result['metrics']['cold_editor_ready_ms'] = (time.perf_counter() - cold_started) * 1000
        result['cold_direct_subprocesses'] = len(processes) - cold_process_start
        phase = 'setup'
        ready = wait_for(lambda state: state['lsp'] == 1 and lsp_pid_file.exists())
        rpc('json_encode(luaeval("(function() vim.api.nvim_buf_set_lines(0,0,-1,false,{\\\"unsaved fixture\\\"}); return true end)()"))')
        before = rpc()
        editor_pid = before['pid']
        lsp_pid = int(lsp_pid_file.read_text())
        agent_before = api('pane', 'process-info', '--pane', agent)['process_info']
        agent_pid = agent_before['shell_pid']
        result['retention_baseline'].update(editor_before=before, editor_pid=editor_pid,
                                            agent_before=agent_before, agent_pid=agent_pid,
                                            lsp_pid=lsp_pid)
        editor_tty, agent_tty = terminal_path(editor_pid), terminal_path(agent_pid)
        api('pane', 'zoom', editor, '--on')
        geometry(editor, editor_tty, time.monotonic(), editor=True)

        call_log = runtime / 'controller-calls.jsonl'
        call_log.write_text('')
        herdr_wrapper = runtime / 'herdr-owned'
        herdr_wrapper.write_text('#!' + sys.executable + '\nimport json,os,sys\n' +
                                 'with open(' + repr(str(call_log)) + ',"a") as log: log.write(json.dumps(sys.argv[1:])+"\\n")\n' +
                                 'os.execv(' + repr(herdr) + ',' + repr([herdr, '--session', session]) + '+sys.argv[1:])\n')
        herdr_wrapper.chmod(0o700)

        def action(name, focused):
            nonlocal editor, phase
            phase = 'hide-command' if name == 'toggle' else 'show-command'
            environment = env | {'HERDR_BIN_PATH': str(herdr_wrapper), 'HERDR_PLUGIN_ROOT': str(source),
                                 'HERDR_PLUGIN_CONFIG_DIR': str(config),
                                 'HERDR_PLUGIN_CONTEXT_JSON': json.dumps({'workspace_id': workspace_id, 'tab_id': tab, 'focused_pane_id': focused})}
            call_start = len(call_log.read_text().splitlines())
            api_start = len(trace)
            process_start = len(processes)
            started = time.monotonic()
            if mode == 'legacy':
                completed = run(['bash', str(source / 'herdr/pane.sh'), name], environment=environment)
                calls = len(call_log.read_text().splitlines()) - call_start
                stdout, stderr = completed.stdout, completed.stderr
            else:
                observed = api('pane', 'get', editor)['pane']
                observation = 'here' if observed['tab_id'] == tab else 'hidden'
                if name == 'toggle':
                    plan('toggle', observation, {'effect': 'park', 'outcome': 'hide', 'retain_guard': True})
                    api('pane', 'zoom', editor, '--off')
                    parked = api('pane', 'move', editor, '--new-tab', '--workspace', workspace_id, '--label', 'PR00 owned parking', '--no-focus')
                    editor = parked['move_result']['pane']['pane_id']
                else:
                    plan('show', observation, {'effect': 'move', 'outcome': 'show', 'retain_guard': True})
                    restored = api('pane', 'move', editor, '--tab', tab, '--split', 'right', '--target-pane', agent, '--focus')
                    editor = restored['move_result']['pane']['pane_id']
                    api('pane', 'zoom', editor, '--on')
                calls = sum('api' in row for row in trace[api_start:])
                stdout = stderr = ''
            metric = {'elapsed_ms': (time.monotonic() - started) * 1000,
                      'external_calls': calls, 'direct_subprocesses': len(processes) - process_start,
                      'count_wrapper_interpreter_starts': calls if mode == 'legacy' else 0,
                      'stdout': stdout, 'stderr': stderr}
            trace.append({'controller_action': name, **metric})
            phase = 'hide-observer' if name == 'toggle' else 'show-observer'
            return started, metric

        hidden_start, hidden = action('toggle', editor)
        editor_after_hide = api('pane', 'get', editor)['pane']
        assert editor_after_hide['tab_id'] != tab, 'one toggle did not hide the editor'
        hide_geometry = geometry(agent, agent_tty, hidden_start, remaining_agent=True)
        hide_ready_ms = (time.monotonic() - hidden_start) * 1000
        shown_start, shown = action('open', agent)
        editor_after_show = api('pane', 'get', editor)['pane']
        assert editor_after_show['tab_id'] == tab, 'open did not restore the editor to the invoking tab'
        show_geometry = geometry(editor, editor_tty, shown_start, editor=True)
        show_ready_ms = (time.monotonic() - shown_start) * 1000
        phase = 'diagnostics'
        after = rpc()
        os.kill(lsp_pid, 0)
        assert after['pid'] == before['pid'] and after['lsp'] == 1 and after['dirty'] and after['lines'] == ['unsaved fixture']
        assert api('pane', 'process-info', '--pane', agent)['process_info'] == agent_before
        assert fixture.read_text() == 'saved fixture\n'
        api('pane', 'send-text', agent, 'fixture-after\n')
        expected_input += b'fixture-after\n'
        result['retention_baseline']['expected_fake_input_prefix'] = {
            'status': 'COMPLETED', 'hex': expected_input.hex(),
            'sha256': hashlib.sha256(expected_input).hexdigest(),
        }
        deadline = time.monotonic() + 1
        expected = expected_input
        while capture.read_bytes() != expected and time.monotonic() < deadline:
            time.sleep(.01)
        assert capture.read_bytes() == expected, 'fake agent input differs'
        roles = {'server': request['server_pid'], 'client': client.pid, 'editor': editor_pid, 'fixture_lsp': lsp_pid, 'fake_agent': agent_pid}
        process_table = run(['ps', '-axo', 'pid=,ppid=,rss=']).stdout
        all_rows = {int(fields[0]): {'ppid': int(fields[1]), 'rss_bytes': int(fields[2]) * 1024}
                    for line in process_table.splitlines() if len(fields := line.split()) == 3}
        retained = {request['server_pid'], client.pid}
        while True:
            children = {pid for pid, row in all_rows.items() if row['ppid'] in retained}
            expanded = retained | children
            if expanded == retained:
                break
            retained = expanded
        rows = {pid: all_rows[pid]['rss_bytes'] for pid in retained if pid in all_rows}
        assert set(roles.values()) <= set(rows), 'an owned role disappeared before resource observation'
        (out / 'owned-process-tree.json').write_text(json.dumps({pid: all_rows[pid] for pid in rows}, indent=2) + '\n')
        result['metrics'].update(warm_show_hide_ms=hide_ready_ms + show_ready_ms,
                                 warm_hide_ms=hide_ready_ms, warm_show_ms=show_ready_ms,
                                 hide_command_ms=hidden['elapsed_ms'], show_command_ms=shown['elapsed_ms'],
                                 external_call_count=hidden['external_calls'] + shown['external_calls'],
                                 hide_external_calls=hidden['external_calls'], show_external_calls=shown['external_calls'],
                                 geometry_settle_ms=max(hide_geometry[-1]['elapsed_ms'], show_geometry[-1]['elapsed_ms']),
                                 process_count=len(rows), rss_bytes=sum(rows.values()))
        result.update(status='PASS', cold_editor=cold, before=before, after=after,
                      hide=hidden, show=shown, hide_geometry=hide_geometry, show_geometry=show_geometry,
                      owned_process_roles=roles, rss_bytes_by_pid=rows,
                      expected_input_hex=expected.hex(), captured_input_hex=capture.read_bytes().hex(),
                      cold_scope='Actual archived herdr/launch.sh with review enabled; timing starts before pane.run and ends at VimEnter, legacy marker, review command registration, and expected target buffer.',
                      source_requires_git_metadata=False,
                      instrumentation={
                          'worker_processes_per_sample': 1,
                          'count_wrapper': 'Legacy only: each Herdr call starts one Python interpreter which records argv and execs the CLI in the same PID. Its startup and file append are inside command timing; no wrapper is used for composition modes.',
                          'warm_command_scope': 'Legacy bash pane.sh including its children, or actual selected planner process plus Python-issued Herdr effects. Direct subprocess argv and phase are recorded; descendants inside archived shell are not independently counted.',
                          'warm_ready_scope': 'Command plus independent pane.get, pane.layout, PTY ioctl, and (show only) fresh nvim --remote-expr processes; diagnostic observer calls are excluded from controller external_call_count.',
                          'geometry_deadline': 'One second from immediately before controller action, never reset for observation or recovery.',
                          'cold_scope': 'Worker, server/client, agent, config and Rust compilation are prepared before timing; pane.run, archived launcher and readiness probes are timed. LSP readiness is verified after the cold metric.',
                          'composition_scope': 'Rust/Lua are real planner processes composed with this common Python effect executor and the archived launcher. They use the same zoomed placement as legacy; they are not production controllers.',
                          'resources_scope': 'Steady server and Herdr client process trees after actions, with five fixture roles identified; includes remaining server-owned shells. Excludes worker and short-lived controller/observer processes; no peak RSS.',
                      })
        (out / 'controller-calls.jsonl').write_bytes(call_log.read_bytes())
        return result
    except Exception as exc:
        retain_failure(result, exc, out, api=api, rpc=rpc,
                       editor=locals().get('editor'), agent=locals().get('agent'),
                       capture=locals().get('capture'), fixture=locals().get('fixture'),
                       retention_baseline=result.get('retention_baseline'))
        return result
    finally:
        if client is not None:
            client.terminate()
            try:
                client.wait(timeout=2)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait(timeout=2)
        stop.set()
        if 'reader' in locals():
            reader.join(timeout=2)
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        result['subprocess_counts_by_phase'] = {name: sum(p['phase'] == name for p in processes) for name in sorted({p['phase'] for p in processes})}
        recovery_calls = {name: sum(row.get('phase') == name and row.get('api', [])[:2] in (['pane', 'resize'], ['pane', 'zoom']) for row in trace) for name in ('hide-observer', 'show-observer')}
        result['recovery_external_calls'] = recovery_calls
        if 'external_call_count' in result['metrics']:
            result['metrics']['external_call_count'] += sum(recovery_calls.values())
            result['metrics']['hide_external_calls'] += recovery_calls['hide-observer']
            result['metrics']['show_external_calls'] += recovery_calls['show-observer']
        result['observer_external_calls'] = {name: sum(p['phase'] == name and p['argv'][:1] == [herdr] for p in processes) for name in ('hide-observer', 'show-observer')}
        (out / 'subprocesses.json').write_text(json.dumps(processes, indent=2) + '\n')
        (out / 'trace.json').write_text(json.dumps(trace, indent=2) + '\n')
        (out / 'measurement.json').write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    measured = measure(json.loads(Path(sys.argv[1]).read_text()))
    sys.exit({'PASS': 0, 'FAIL': 1, 'UNVERIFIED': 2}[measured['status']])
