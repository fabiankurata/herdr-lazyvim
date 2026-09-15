"""Interleaved exact-source measurements with the current instrumentation.

Prototype integration: call run_pairs(..., measure=measure_lifecycle, real=True).
measure(source, owned_session, sample_artifact) executes ONE lifecycle and returns
its numeric metrics and observed outcome. The same callable runs on both archives.
It must use owned_session.execute/run for child processes and API calls. This API
collects observations; it does not certify PR00 or turn a callback's label into PASS.
"""
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "live"))
from fake_model import initial_state
from owned_session import OwnedSession, OwnershipError, write_json, exception_evidence
from source import exact_revision, git, new_artifact, source_archive

HARNESS = Path(__file__).resolve().parents[2]
REQUIRED = ["warm_show_hide_ms", "cold_editor_ready_ms", "external_call_count",
            "geometry_settle_ms", "process_count", "rss_bytes"]


def shell_open(source, runtime, artifact):
    """Only pane.sh dispatch overhead against a fake API; no editor is created."""
    model = runtime.root / "model.json"
    calls = runtime.root / "calls.bin"
    write_json(model, initial_state())
    config = runtime.root / "plugin-config"
    config.mkdir()
    env = runtime.env | {
        "HERDR_BIN_PATH": str(HARNESS / "tests/live/fake-herdr"),
        "HERDR_PLUGIN_ROOT": str(source), "HERDR_PLUGIN_CONFIG_DIR": str(config),
        "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace_id": "w", "tab_id": "w:t1", "focused_pane_id": "w:p1"}),
        "HERDR_LAZYVIM_SLEEP_BIN": "true", "HERDR_TEST_MODEL": str(model),
        "HERDR_TEST_CALLS": str(calls),
    }
    started = time.perf_counter_ns()
    completed = runtime.execute(["bash", str(source / "herdr/pane.sh"), "open"], env=env, check=False)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    state = json.loads(model.read_text())
    (artifact / "stdout.txt").write_text(completed.stdout)
    (artifact / "stderr.txt").write_text(completed.stderr)
    (artifact / "calls.bin").write_bytes(calls.read_bytes() if calls.exists() else b"")
    write_json(artifact / "model.json", state)
    created = len(state["panes"]) == 2 and state["panes"]["w:p1"]["process_id"] == "fake-agent-1"
    return {"status": "PASS" if completed.returncode == 0 and created else "FAIL",
            "exit_code": completed.returncode, "fake_editor_created": created,
            "metrics": {"shell_dispatch_open_ms": elapsed, "external_call_count": len(state["calls"])}}


def summary(observations, label):
    selected = [item for item in observations if item["side"] == label]
    passed = [item for item in selected if item["status"] == "PASS"]
    names = sorted({key for item in passed for key in item.get("metrics", {})})
    metrics = {}
    for name in names:
        values = [item["metrics"][name] for item in passed if name in item.get("metrics", {})]
        metrics[name] = {"count": len(values), "p50": statistics.median(values),
                         "p95": sorted(values)[math.ceil(len(values) * 0.95) - 1]}
    return {"attempted": len(selected), "passed": len(passed),
            "failed": len(selected) - len(passed), "metrics": metrics}


def run_pairs(repo, baseline, candidate, samples, artifact, *, measure=shell_open, real=False):
    """Run AB, BA pairs; retain each sample, including exceptions and timeouts."""
    observations = []
    with source_archive(repo, baseline) as (base_source, base_info):
        with source_archive(repo, candidate) as (head_source, head_info):
            sources = {"baseline": (base_source, base_info), "candidate": (head_source, head_info)}
            write_json(artifact / "sources.json", {side: info for side, (_, info) in sources.items()})
            for index in range(samples):
                order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                for side in order:
                    source, info = sources[side]
                    sample_artifact = new_artifact(artifact / f"sample-{index:03d}-{side}")
                    observation = {"index": index, "order": len(observations), "side": side, **info}
                    runtime = OwnedSession(sample_artifact, real=real,
                                           disable_update_checks=True)
                    ownership_failure = None
                    try:
                        with runtime:
                            measured = measure(source, runtime, sample_artifact)
                            if set(measured) & set(observation):
                                raise ValueError("workload cannot replace source or sample identities")
                            if measured.get("status") not in {"PASS", "FAIL", "UNVERIFIED"}:
                                raise ValueError("workload did not provide an observed status")
                            for value in measured.get("metrics", {}).values():
                                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                                    raise ValueError("metrics must be finite nonnegative numbers")
                        # A failed teardown invalidates the sample as well.
                        observation.update(measured)
                    except OwnershipError as exc:
                        observation.update(status="FAIL", **exception_evidence(exc))
                        ownership_failure = exc
                    except Exception as exc:
                        observation.update(status="FAIL", **exception_evidence(exc))
                    except BaseException as exc:
                        observation.update(status="FAIL", **exception_evidence(exc))
                        observations.append(observation)
                        write_json(artifact / "observations.json", observations)
                        raise
                    if runtime.root is not None and runtime.root.exists():
                        # A failed teardown must retain executable source too.
                        base_info["retain_archive"] = True
                        head_info["retain_archive"] = True
                    observations.append(observation)
                    write_json(sample_artifact / "result.json", observation)
                    write_json(artifact / "observations.json", observations)
                    if ownership_failure is not None:
                        write_json(artifact / "sources.json", {side: info for side, (_, info) in sources.items()})
                        raise ownership_failure
            write_json(artifact / "sources.json", {side: info for side, (_, info) in sources.items()})
    return observations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--samples", type=int, choices=(30, 100), default=30)
    parser.add_argument("--artifact-dir", type=Path)
    args = parser.parse_args()
    artifact = None
    try:
        baseline = exact_revision(HARNESS, args.baseline)
        candidate = exact_revision(HARNESS, args.candidate)
        artifact = new_artifact(args.artifact_dir or HARNESS / "artifacts/PR00" / args.candidate / ("perf-" + str(time.time_ns())))
        result = {
            "status": "UNVERIFIED", "scenario": args.scenario,
            "sources": {"baseline": baseline, "candidate": candidate},
            "harness": {"revision": git(HARNESS, "rev-parse", "HEAD"),
                        "dirty": bool(git(HARNESS, "status", "--porcelain"))},
            "requested_samples_per_side": args.samples,
            "unsupported_required_metrics": REQUIRED,
            "pr00_perf": "UNVERIFIED", "native_cmd_keys": "UNVERIFIED",
        }
        if args.scenario == "controller-choice":
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "pr00_live_bridge", HARNESS / "tests/prototypes/compare_live.py"
            )
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
            result = bridge.compare_workload(
                sys.modules[__name__], HARNESS, args.baseline,
                args.candidate, args.samples, artifact,
            )
            print(artifact / "result.json")
            return {"PASS": 0, "FAIL": 1, "UNVERIFIED": 2}[result["status"]]
        if args.scenario != "shell-open":
            result["reason"] = "No integrated actual lifecycle workload. Use run_pairs with the prototype lifecycle callable. No unrelated workload was measured."
            write_json(artifact / "result.json", result)
            print(artifact / "result.json")
            return 2
        result["workload"] = "shell dispatch open against evolving fake pane API; does not create a live editor"
        result["order"] = "interleaved AB, BA pairs"
        write_json(artifact / "result.json", result)
        observations = run_pairs(HARNESS, args.baseline, args.candidate, args.samples, artifact)
        result["observations"] = observations
        result["measured"] = {side: summary(observations, side) for side in ("baseline", "candidate")}
        failures = [item for item in observations if item["status"] != "PASS"]
        result["shell_workload_status"] = "FAIL" if failures else "PASS"
        if failures:
            result["status"] = "FAIL"
        # No speedup is reported against a failed baseline or a different outcome.
        write_json(artifact / "baseline.json", {"source": baseline, **result["measured"]["baseline"]})
        write_json(artifact / "result.json", result)
        print(artifact / "result.json")
        return 1 if failures else 0
    except BaseException as exc:
        if artifact is not None:
            failure = {"status": "FAIL", **exception_evidence(exc),
                       "partial_observations": "observations.json"}
            write_json(artifact / "failure.json", failure)
            write_json(artifact / "result.json", failure)
        print(exception_evidence(exc)["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
