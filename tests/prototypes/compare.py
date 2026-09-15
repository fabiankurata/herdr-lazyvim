#!/usr/bin/env python3
"""Compare isolated planner processes. No Herdr commands or editor lifecycle effects."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact-dir', type=Path, required=True)
    parser.add_argument('--samples', type=int, default=30)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    out = args.artifact_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    binary = out / 'controller-rust'
    subprocess.run(['rustc', '-O', str(ROOT / 'controller.rs'), '-o', str(binary)], check=True)
    runtime = out / 'runtime'
    runtime.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('HERDR_', 'NVIM_', 'XDG_'))}
    env.update(HOME=str(runtime), XDG_CONFIG_HOME=str(runtime / 'config'),
               XDG_DATA_HOME=str(runtime / 'data'), XDG_STATE_HOME=str(runtime / 'state'),
               XDG_CACHE_HOME=str(runtime / 'cache'))
    commands = {'rust': [str(binary)], 'lua': ['nvim', '--headless', '-u', 'NONE', '-i', 'NONE', '-l', str(ROOT / 'controller.lua')]}

    def run(runtime_name, request):
        start = time.perf_counter_ns()
        p = subprocess.run(commands[runtime_name] + request, env=env, capture_output=True, text=True, timeout=5)
        elapsed = (time.perf_counter_ns() - start) / 1e6
        return p.returncode, json.loads(p.stdout), elapsed

    cases = [
        (['show', 'absent', 'idle', 'unverified'], ('spawn', 'show', True)),
        (['hide', 'absent', 'idle', 'unverified'], ('none', 'hide', False)),
        (['toggle', 'here', 'idle', 'verified'], ('park', 'hide', True)),
        (['toggle', 'elsewhere', 'idle', 'verified'], ('move', 'show', True)),
        (['show', 'here', 'idle', 'verified'], ('none', 'show', False)),
        (['show', 'hidden', 'idle', 'verified'], ('move', 'show', True)),
        (['hide', 'hidden', 'idle', 'verified'], ('none', 'hide', False)),
        (['show', 'ambiguous', 'idle', 'verified'], ('none', 'needs_attention', True)),
        (['hide', 'here', 'idle', 'unverified'], ('none', 'needs_attention', True)),
        (['toggle', 'here', 'uncertain_hide', 'verified'], ('observe', 'uncertain', True)),
        (['toggle', 'hidden', 'uncertain_hide', 'verified'], ('observe', 'uncertain', True)),
        (['toggle', 'here', 'uncertain_show', 'verified'], ('observe', 'uncertain', True)),
        (['toggle', 'hidden', 'confirmed_hide', 'verified'], ('none', 'hide', False)),
        (['toggle', 'here', 'confirmed_show', 'verified'], ('none', 'show', False)),
    ]
    traces = []
    for request, expected_tuple in cases:
        expected = dict(zip(('effect', 'outcome', 'retain_guard'), expected_tuple))
        for name in commands:
            code, actual, elapsed = run(name, request)
            assert (code, actual) == (0, expected), (name, request, actual)
            traces.append(dict(runtime=name, request=request, response=actual, elapsed_ms=elapsed))
    for request, code in [([], 'invalid_request'), (['bad', 'here', 'idle', 'verified'], 'invalid_action'),
                          (['show', 'bad', 'idle', 'verified'], 'invalid_observation'),
                          (['show', 'here', 'bad', 'verified'], 'invalid_progress'),
                          (['show', 'here', 'idle', 'bad'], 'invalid_ownership')]:
        for name in commands:
            status, actual, _ = run(name, request)
            assert (status, actual) == (2, {'error': code}), (name, request, actual)

    # Simulate a server effect that remains outstanding across controller restarts.
    for name in commands:
        _, first, _ = run(name, ['toggle', 'here', 'idle', 'verified'])
        assert first == {'effect': 'park', 'outcome': 'hide', 'retain_guard': True}
        for observation in ('here', 'hidden'):
            _, recovery, _ = run(name, ['toggle', observation, 'uncertain_hide', 'verified'])
            assert recovery == {'effect': 'observe', 'outcome': 'uncertain', 'retain_guard': True}
        _, confirmed, _ = run(name, ['toggle', 'hidden', 'confirmed_hide', 'verified'])
        assert confirmed == {'effect': 'none', 'outcome': 'hide', 'retain_guard': False}
        traces.append(dict(runtime=name, scenario='late_effect_model', response=confirmed,
                           limitation='Observations are synthetic. No real process or durable lock is tested.'))

    measurements = {name: [] for name in commands}
    for sample in range(args.samples):
        for name in (list(commands) if sample % 2 == 0 else list(reversed(commands))):
            code, actual, elapsed = run(name, ['toggle', 'here', 'idle', 'verified'])
            assert (code, actual) == (0, {'effect': 'park', 'outcome': 'hide', 'retain_guard': True})
            measurements[name].append(elapsed)
    summary = {name: {'p50_ms': statistics.median(values),
                      'p95_ms': sorted(values)[max(0, (95 * len(values) + 99) // 100 - 1)],
                      'samples_ms': values} for name, values in measurements.items()}
    result = {'status': 'PASS', 'scope': 'pure planning and fresh process startup only',
              'live_controller': 'UNVERIFIED', 'cases_per_runtime': len(cases) + 5,
              'latency': summary, 'rust_binary_bytes': binary.stat().st_size,
              'external_calls_per_action': 0, 'trace': traces}
    (out / 'comparison.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('trace', 'latency')}))


if __name__ == '__main__':
    main()
