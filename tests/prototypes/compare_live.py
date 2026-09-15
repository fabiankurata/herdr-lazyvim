#!/usr/bin/env python3
"""Bridge the shared AB/BA comparator to one archived live launcher workload."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def load_comparator(harness):
    path = harness / 'tests/perf/compare.py'
    spec = importlib.util.spec_from_file_location('pr00_comparator', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def measure_lifecycle(source, runtime, artifact, *, runtime_mode="legacy", rustc=None):
    """Use the existing owned server and supervise the entire sample subprocess."""
    request = runtime.root / 'live-measure-request.json'
    request.write_text(json.dumps({'source': str(source), 'artifact': str(artifact),
                                   'runtime_root': str(runtime.root), 'socket': str(runtime.socket),
                                   'session': runtime.session, 'herdr': runtime.herdr,
                                   'server_pid': runtime.proc.pid, 'runtime_mode': runtime_mode,
                                   'rustc': rustc}) + '\n')
    completed = runtime.execute([sys.executable, str(ROOT / 'measure_live.py'), str(request)], check=False, timeout=30)
    (artifact / 'worker-stdout.txt').write_text(completed.stdout)
    (artifact / 'worker-stderr.txt').write_text(completed.stderr)
    receipt = artifact / 'measurement.json'
    if not receipt.exists():
        return {'status': 'FAIL', 'error': 'sample worker produced no receipt', 'worker_exit': completed.returncode, 'metrics': {}}
    result = json.loads(receipt.read_text())
    result['worker_exit'] = completed.returncode
    if completed.returncode and not (completed.returncode == 2 and result['status'] == 'UNVERIFIED'):
        result['status'] = 'FAIL'
    return result


def resolve_rustc(artifact):
    direct = shutil.which('rustc')
    selected = direct
    if direct and Path(direct).resolve().name == 'rustup':
        selected = subprocess.run(['rustup', 'which', 'rustc'], text=True, capture_output=True, check=True).stdout.strip()
    if not selected or not Path(selected).is_file():
        raise RuntimeError('no concrete rustc executable is available outside isolation')
    selected = str(Path(selected).resolve())
    version = subprocess.run([selected, '--version'], text=True, capture_output=True, check=True).stdout.strip()
    sysroot = subprocess.run([selected, '--print', 'sysroot'], text=True, capture_output=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='pr00-rustc-probe-') as directory:
        root = Path(directory)
        source, binary = root / 'probe.rs', root / 'probe'
        source.write_text('fn main(){print!("ok")}')
        isolated = {'PATH': os.environ['PATH'], 'HOME': str(root / 'home'), 'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_DATA_HOME': str(root / 'data'), 'XDG_STATE_HOME': str(root / 'state')}
        subprocess.run([selected, str(source), '-o', str(binary)], env=isolated, check=True, capture_output=True, text=True)
        assert subprocess.run([str(binary)], text=True, capture_output=True, check=True).stdout == 'ok'
    provenance = {'path': selected, 'version': version, 'sysroot': sysroot, 'probe': 'PASS'}
    comparator_write = getattr(artifact, 'write_text')
    comparator_write(json.dumps(provenance, indent=2) + '\n')
    return provenance


def compare_workload(comparator, repo, baseline, candidate, samples, artifact, runtime_mode="legacy"):
    """Shared entrypoint for this CLI and the coordinator's controller-choice CLI."""
    files = [ROOT / name for name in ('compare_live.py', 'measure_live.py', 'fake_agent.py', 'fake_lsp.py', 'controller.rs', 'controller.lua')]
    files += [Path(comparator.__file__), Path(comparator.__file__).parents[1] / 'live/owned_session.py']
    comparator.write_json(artifact / 'instrumentation.json', {
        str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in files})
    rustc = None
    if runtime_mode == 'rust':
        rustc = resolve_rustc(artifact / 'rustc-provenance.json')['path']
    def measure(source, runtime, sample_artifact):
        return measure_lifecycle(source, runtime, sample_artifact, runtime_mode=runtime_mode, rustc=rustc)

    observations = comparator.run_pairs(repo, baseline, candidate, samples, artifact,
                                        measure=measure, real=True)
    failures = [item for item in observations if item['status'] == 'FAIL']
    unverified = [item for item in observations if item['status'] == 'UNVERIFIED']
    missing = sorted({name for item in observations for name in comparator.REQUIRED if name not in item.get('metrics', {})})
    status = 'FAIL' if failures else ('UNVERIFIED' if missing or unverified or samples < 30 else 'PASS')
    result = {'status': status, 'scenario': 'controller-choice', 'kind': 'matched-live-launcher', 'runtime_mode': runtime_mode,
              'order': 'sample-by-sample AB, BA', 'samples_per_side': samples,
              'required_metrics_missing': missing,
              'measured': {side: comparator.summary(observations, side) for side in ('baseline', 'candidate')},
              'failed_samples': [{'side': item['side'], 'index': item['index'], 'error': item.get('error')} for item in failures],
              'unverified_samples': [{'side': item['side'], 'index': item['index'], 'error': item.get('error')} for item in unverified],
              'native_cmd_keys': 'UNVERIFIED', 'pr00_acceptance': 'UNVERIFIED',
              'scope': 'Same archived launch.sh and pane.sh workload with review enabled in an isolated plain profile. Cold readiness includes pane.run through launcher completion; plugin-registry opening and dependency downloads are excluded.'}
    result['provisional_warm_budget'] = {side: {name: {
        'p95_ms': metric['p95'], 'budget_ms': 500,
        'observed': 'PASS' if metric['p95'] <= 500 else 'FAIL',
        'required_sample_count_met': metric['count'] >= 30}
        for name, metric in summary['metrics'].items() if name in ('warm_hide_ms', 'warm_show_ms')}
        for side, summary in result['measured'].items()}
    comparator.write_json(artifact / 'baseline.json', result['measured']['baseline'])
    comparator.write_json(artifact / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', type=Path, default=ROOT.parents[1])
    parser.add_argument('--harness-root', type=Path, default=ROOT.parents[1])
    parser.add_argument('--baseline', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--runtime', choices=('legacy', 'rust', 'lua'), default='legacy')
    parser.add_argument('--samples', type=int, default=30)
    parser.add_argument('--artifact-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    comparator = load_comparator(args.harness_root.resolve())
    repo = args.repo.resolve()
    comparator.exact_revision(repo, args.baseline)
    comparator.exact_revision(repo, args.candidate)
    artifact = comparator.new_artifact(args.artifact_dir)
    try:
        result = compare_workload(comparator, repo, args.baseline, args.candidate, args.samples, artifact, args.runtime)
        print(artifact / 'result.json')
        return {'PASS': 0, 'FAIL': 1, 'UNVERIFIED': 2}[result['status']]
    except BaseException as exc:
        comparator.write_json(artifact / 'result.json', {'status': 'FAIL', 'error': repr(exc), 'partial_observations': 'observations.json'})
        raise


if __name__ == '__main__':
    sys.exit(main())
