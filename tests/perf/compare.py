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


def comment_operations(source, runtime, artifact):
    """Measure public list/send/ack against an archived plugin source and fake delivery."""
    measurements = {}
    for count in (10, 1000):
        result_path = runtime.root / f"comment-operations-{count}.json"
        fixture = runtime.root / f"comment-operations-fixture-{count}"
        env = runtime.env | {
            "SOURCE_REPO": str(source), "FIXTURE_ROOT": str(fixture),
            "HERDR_TEST_STATE_ROOT": str(runtime.root / f"state-{count}"),
            "OPERATION_COUNT": str(count), "OPERATION_RESULT": str(result_path),
            "HERDR_NVIM_TEST_SCRIPT": str(HARNESS / "tests/nvim/comment_operations.lua"),
        }
        completed = runtime.execute([
            "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l", str(HARNESS / "tests/nvim/run_test.lua"),
        ], env=env, check=False, timeout=20)
        (artifact / f"stdout-{count}.txt").write_text(completed.stdout)
        (artifact / f"stderr-{count}.txt").write_text(completed.stderr)
        if completed.returncode != 0 or not result_path.exists():
            return {"status": "FAIL", "exit_code": completed.returncode, "metrics": {}}
        observed = json.loads(result_path.read_text())
        measurements.update(comment_operation_metrics(observed, count))
    return {"status": "PASS", "metrics": measurements}


def comment_operation_metrics(observed, count):
    """Validate and namespace the Lua fixture's public-operation timings."""
    required = ("add_bookkeeping_ms", "add_total_ms", "list_bookkeeping_ms",
                "list_total_ms", "send_ack_bookkeeping_ms", "send_ack_total_ms")
    if observed.get("status") != "PASS" or observed.get("seeded_count") != count:
        raise ValueError("comment operation fixture did not complete its requested public flow")
    if observed.get("listed_count") != count + 1:
        raise ValueError("comment operation fixture did not list the public add")
    if observed.get("fake_delivery_delay_ms") != 0:
        raise ValueError("comment operation fixture must exclude fake delivery delay")
    metrics = {}
    for key in required:
        value = observed.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid comment operation metric: " + key)
        metrics[f"{key[:-3]}_{count}_ms"] = value
    return metrics


def package_extraction_metrics(observed):
    """Validate the PR02 legacy fixture and expose its like-for-like timings."""
    required = ("module_setup_ms", "sequential_crud_ms", "export_ms",
                "old_format_store_access_ms")
    byte_fields = ("old_store_bytes", "old_store_after_access_bytes",
                   "final_store_bytes", "export_payload")
    if observed.get("status") != "PASS":
        raise ValueError("package extraction fixture did not complete its public flow")
    if observed.get("export_entrypoint") != "herdr_review.send":
        raise ValueError("package extraction fixture did not use the legacy public export path")
    if observed.get("old_store_bytes") == observed.get("old_store_after_access_bytes"):
        raise ValueError("old-format store access did not exercise legacy normalization")
    for key in byte_fields:
        if not isinstance(observed.get(key), str):
            raise ValueError("package extraction fixture omitted literal " + key)
    metrics = {}
    for key in required:
        value = observed.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid package extraction metric: " + key)
        metrics[key] = value
    return metrics


def package_extraction_pair(sources, runtime, artifact, order):
    """Run both exact-source arms in one Neovim VM for each AB/BA pair."""
    result_path = runtime.root / "package-extraction-pair.json"
    fixture = artifact.parent / "fixture-roots" / artifact.name
    env = runtime.env | {
        "HARNESS": str(HARNESS), "FIXTURE_ROOT": str(fixture),
        "HERDR_TEST_STATE_ROOT": str(runtime.root / "state"),
        "OPERATION_RESULT": str(result_path), "PAIR_ORDER": ",".join(order),
        "PACKAGE_EXTRACTION_LIBRARY": "1",
        "BASELINE_SOURCE_REPO": str(sources["baseline"]),
        "CANDIDATE_SOURCE_REPO": str(sources["candidate"]),
    }
    completed = runtime.execute([
        "nvim", "--headless", "-u", "NONE", "-i", "NONE", "-l",
        str(HARNESS / "tests/nvim/package_extraction_pair.lua"),
    ], env=env, check=False, timeout=20)
    (artifact / "stdout.txt").write_text(completed.stdout)
    (artifact / "stderr.txt").write_text(completed.stderr)
    if completed.returncode != 0 or not result_path.exists():
        return {"status": "FAIL", "exit_code": completed.returncode, "arms": {}}
    observed = json.loads(result_path.read_text())
    (artifact / "fixture-result.json").write_text(json.dumps(observed, indent=2) + "\n")
    if observed.get("status") != "PASS" or observed.get("order") != ",".join(order):
        raise ValueError("same-VM package extraction fixture did not complete its requested pair")
    arms = observed.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"baseline", "candidate"}:
        raise ValueError("same-VM package extraction fixture omitted an arm")
    return {"status": "PASS", "arms": {
        side: {"status": "PASS", "metrics": package_extraction_metrics(arms[side]),
               "behavior": {key: arms[side][key] for key in (
                   "old_store_bytes", "old_store_after_access_bytes",
                   "final_store_bytes", "export_payload",
               )}}
        for side in ("baseline", "candidate")
    }, "reset_boundary": observed.get("reset_boundary")}


def run_package_extraction_pairs(repo, baseline, candidate, samples, artifact):
    """Keep source arms in one VM while preserving AB/BA observation order."""
    observations = []
    with source_archive(repo, baseline) as (base_source, base_info):
        with source_archive(repo, candidate) as (head_source, head_info):
            sources = {"baseline": (base_source, base_info), "candidate": (head_source, head_info)}
            write_json(artifact / "sources.json", {side: info for side, (_, info) in sources.items()})
            for index in range(samples):
                order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                sample = new_artifact(artifact / f"sample-{index:03d}-pair")
                runtime = OwnedSession(sample, real=False, disable_update_checks=True)
                ownership_failure = None
                try:
                    with runtime:
                        measured = package_extraction_pair(
                            {side: source for side, (source, _) in sources.items()}, runtime, sample, order)
                    if measured["status"] != "PASS":
                        raise RuntimeError("same-VM package extraction fixture failed")
                    for position, side in enumerate(order):
                        info = sources[side][1]
                        arm = measured["arms"][side]
                        observations.append({"index": index, "order": len(observations), "pair_order": list(order),
                                             "same_vm_pair": True, "pair_position": position, "side": side,
                                             **info, **arm})
                except OwnershipError as exc:
                    error = exception_evidence(exc)
                    ownership_failure = exc
                    for position, side in enumerate(order):
                        info = sources[side][1]
                        observations.append({"index": index, "order": len(observations), "pair_order": list(order),
                                             "same_vm_pair": True, "pair_position": position, "side": side,
                                             **info, "status": "FAIL", **error})
                except Exception as exc:
                    error = exception_evidence(exc)
                    for position, side in enumerate(order):
                        info = sources[side][1]
                        observations.append({"index": index, "order": len(observations), "pair_order": list(order),
                                             "same_vm_pair": True, "pair_position": position, "side": side,
                                             **info, "status": "FAIL", **error})
                write_json(sample / "result.json", {"observations": observations[-2:]})
                write_json(artifact / "observations.json", observations)
                if ownership_failure is not None:
                    write_json(artifact / "sources.json", {side: info for side, (_, info) in sources.items()})
                    raise ownership_failure
            write_json(artifact / "sources.json", {side: info for side, (_, info) in sources.items()})
    return observations


def package_extraction_equivalence(observations):
    """Compare literal outputs for each interleaved PR02 baseline/candidate pair."""
    fields = ("old_store_bytes", "old_store_after_access_bytes",
              "final_store_bytes", "export_payload")
    pairs = {}
    for observation in observations:
        pairs.setdefault(observation["index"], {})[observation["side"]] = observation
    mismatches = []
    for index, pair in sorted(pairs.items()):
        if set(pair) != {"baseline", "candidate"}:
            mismatches.append({"index": index, "reason": "missing comparison arm"})
            continue
        if pair["baseline"].get("status") != "PASS" or pair["candidate"].get("status") != "PASS":
            continue
        different = [field for field in fields if pair["baseline"].get("behavior", {}).get(field) != pair["candidate"].get("behavior", {}).get(field)]
        if different:
            mismatches.append({"index": index, "fields": different})
    return mismatches


def package_extraction_regressions(measured):
    """Apply the PR02 p95 budget to equivalent successful operations."""
    baseline = measured["baseline"]["metrics"]
    candidate = measured["candidate"]["metrics"]
    regressions = []
    for name in ("module_setup_ms", "sequential_crud_ms", "export_ms", "old_format_store_access_ms"):
        if name not in baseline or name not in candidate:
            regressions.append({"metric": name, "reason": "missing successful p95"})
            continue
        baseline_p95 = baseline[name]["p95"]
        candidate_p95 = candidate[name]["p95"]
        budget = baseline_p95 * 1.25 + 20
        if candidate_p95 > budget:
            regressions.append({"metric": name, "baseline_p95": baseline_p95,
                                "candidate_p95": candidate_p95, "budget": budget})
    return regressions


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
    parser.add_argument("--samples", type=int, choices=(1, 30, 100), default=30,
                        help="1 is a smoke check; comparable evidence requires 30 or 100")
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
        if args.scenario not in {"shell-open", "comment-operations", "package-extraction"}:
            result["reason"] = "No integrated actual lifecycle workload. Use run_pairs with the prototype lifecycle callable. No unrelated workload was measured."
            write_json(artifact / "result.json", result)
            print(artifact / "result.json")
            return 2
        if args.scenario == "comment-operations":
            result["workload"] = "public list/send/ack against a fixture-only delivery command; fake delivery delay excluded"
            result["order"] = "interleaved AB, BA pairs"
            write_json(artifact / "result.json", result)
            observations = run_pairs(HARNESS, args.baseline, args.candidate, args.samples, artifact,
                                     measure=comment_operations)
            result["observations"] = observations
            result["measured"] = {side: summary(observations, side) for side in ("baseline", "candidate")}
            failures = [item for item in observations if item["status"] != "PASS"]
            result["status"] = "FAIL" if failures else "PASS"
            result["comment_operations_status"] = result["status"]
            write_json(artifact / "baseline.json", {"source": baseline, **result["measured"]["baseline"]})
            write_json(artifact / "result.json", result)
            print(artifact / "result.json")
            return 1 if failures else 0
        if args.scenario == "package-extraction":
            result["workload"] = (
                "legacy module setup plus public comment/list/edit/delete/send; records literal "
                "old-format and resulting store bytes in one Neovim VM per pair"
            )
            result["order"] = "interleaved AB, BA pairs"
            result["uuid_control"] = "fixture-local deterministic vim.uv.random sequence"
            write_json(artifact / "result.json", result)
            observations = run_package_extraction_pairs(HARNESS, args.baseline, args.candidate, args.samples, artifact)
            result["observations"] = observations
            result["measured"] = {side: summary(observations, side) for side in ("baseline", "candidate")}
            result["literal_equivalence_mismatches"] = package_extraction_equivalence(observations)
            result["p95_regressions"] = package_extraction_regressions(result["measured"])
            failures = [item for item in observations if item["status"] != "PASS"]
            result["status"] = "FAIL" if failures or result["literal_equivalence_mismatches"] or result["p95_regressions"] else "PASS"
            result["package_extraction_status"] = result["status"]
            first_baseline = next((item["behavior"] for item in observations
                                   if item["side"] == "baseline" and item["status"] == "PASS"), None)
            baseline_receipt = {"source": baseline, **result["measured"]["baseline"]}
            if first_baseline is not None:
                baseline_receipt["literal_behavior"] = first_baseline
            write_json(artifact / "baseline.json", baseline_receipt)
            write_json(artifact / "result.json", result)
            print(artifact / "result.json")
            return 1 if result["status"] == "FAIL" else 0
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


def owned_main():
    """Run the public comparator as a lease owner or verified borrower."""
    from runtime_lease import runtime_lease_owner
    with runtime_lease_owner(repo=HARNESS):
        return main()


if __name__ == "__main__":
    sys.exit(owned_main())
