#!/usr/bin/env python3
"""Bounded PR00 lanes; scenario names never substitute for observations."""
import argparse
import contextlib
import json
import io
from pathlib import Path
import runpy
import signal
import subprocess
import sys

from owned_session import OwnedSession, write_json, exception_evidence
from source import checked_source, git, new_artifact, source_archive


@contextlib.contextmanager
def cancellation():
    def interrupted(signum, frame):
        raise KeyboardInterrupt("signal " + str(signum))
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-mode", choices=("checkout", "archive"), default="checkout")
    parser.add_argument("--source-repo", type=Path)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--socket")
    args = parser.parse_args()
    harness = Path(__file__).resolve().parents[2]
    repo = args.source_repo or harness
    artifact = None
    try:
        with cancellation(), contextlib.ExitStack() as stack:
            if args.source_mode == "archive":
                source, provenance = stack.enter_context(source_archive(repo, args.revision))
            else:
                provenance = checked_source(repo, args.revision)
                source = repo
            artifact = new_artifact(args.artifact_dir)
            if args.session or args.socket:
                raise ValueError("caller socket or session is forbidden")
            instrumentation = {"revision": git(harness, "rev-parse", "HEAD"),
                               "dirty": bool(git(harness, "status", "--porcelain"))}
            write_json(artifact / "source.json", {"source": provenance, "harness": instrumentation})
            supported = (args.scenario == "boot-isolation" and args.real) or (args.scenario == "isolation" and not args.real)
            dispatches_workload = args.scenario in {"live-pane", "remote-ui", "server-identity", "editor-profiles"} and args.real
            workload_uses_live_herdr = False
            with (contextlib.nullcontext() if dispatches_workload else OwnedSession(
                artifact, real=args.real and supported,
                session=args.session, socket=args.socket,
            )) as runtime:
                if args.scenario == "boot-isolation" and args.real:
                    workload_uses_live_herdr = True
                    result = {"status": "PASS", "scope": "real named server boot, API snapshot and teardown"}
                elif args.scenario == "isolation" and not args.real:
                    runtime.execute([str(harness / "tests/fixtures/create"), str(runtime.root / "fixture")], timeout=10)
                    runtime.execute([sys.executable, str(harness / "tests/live/pty_driver.py"),
                                     str(artifact / "capture/pty")], timeout=5)
                    result = {"status": "PASS", "scope": "synthetic repositories and deterministic PTY byte/resize capture"}
                elif args.scenario in {"live-pane", "remote-ui", "server-identity", "editor-profiles"} and args.real:
                    if args.scenario == "live-pane" and args.source_mode != "checkout":
                        result = {"status": "UNVERIFIED", "reason": "live-pane requires an exact checked-out source; an archive has no Git HEAD"}
                    else:
                        workload_uses_live_herdr = args.scenario in {"live-pane", "server-identity"}
                        scripts = {
                            "live-pane": harness / "tests/prototypes/live_pane.py",
                            "remote-ui": harness / "tests/prototypes/remote_ui.py",
                            "server-identity": harness / "tests/prototypes/server_probe.py",
                            "editor-profiles": harness / "tests/profiles/run.py",
                        }
                        command = [str(scripts[args.scenario]), "--artifact-dir", str(artifact / "workload")]
                        if args.scenario == "live-pane":
                            command = [str(scripts[args.scenario]), "--artifact-dir", str(artifact / "workload"), "--runner-dir", str(harness / "tests/live"), "--cycles", "100", "--extra-scenarios", "--resize-recovery", "nudge"]
                        elif args.scenario == "server-identity":
                            command.extend(["--runner-dir", str(harness / "tests/live")])
                        elif args.scenario == "editor-profiles":
                            command = [str(scripts[args.scenario]), str(artifact / "workload"), "--revision", args.revision, "--source-mode", args.source_mode, "--source-repo", str(repo)]
                        output = io.StringIO()
                        old_argv = sys.argv
                        try:
                            sys.argv = command
                            with contextlib.redirect_stdout(output):
                                runpy.run_path(command[0], run_name="__main__")
                            exit_code = 0
                        except SystemExit as exit_signal:
                            exit_code = int(exit_signal.code or 0)
                        finally:
                            sys.argv = old_argv
                        (artifact / "workload-stdout.txt").write_text(output.getvalue())
                        workload = artifact / "workload" / ("results.json" if args.scenario == "editor-profiles" else "result.json")
                        observed = json.loads(workload.read_text()) if workload.exists() else {"status": "FAIL", "reason": "workload produced no result"}
                        result = {"status": "FAIL" if exit_code else observed.get("status", "FAIL"), "scope": observed.get("scope", "narrow workload"), "workload_exit_code": exit_code}
                else:
                    result = {"status": "UNVERIFIED", "reason": "no implemented workload for this scenario/mode"}
            # PASS is written only after context teardown and identity comparison.
            result.update(scenario=args.scenario, source=provenance,
                          live_herdr=workload_uses_live_herdr,
                          native_cmd_keys="UNVERIFIED", screenshots="UNVERIFIED",
                          pr00_live_acceptance="UNVERIFIED")
            write_json(artifact / "result.json", result)
            print(json.dumps(result))
            return 0 if result["status"] == "PASS" else 2
    except BaseException as exc:
        if artifact is not None:
            write_json(artifact / "result.json", {"status": "FAIL", "scenario": args.scenario,
                                                 **exception_evidence(exc)})
        print(exception_evidence(exc)["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    from runtime_lease import runtime_lease_owner
    with runtime_lease_owner(repo=Path(__file__).resolve().parents[2]):
        sys.exit(main())
