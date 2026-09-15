"""Present two owned server namespaces through one owned fixture command."""
import json
from pathlib import Path
import subprocess
import sys
import hashlib
import argparse
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'live'))
from source import checked_source, new_artifact
from owned_session import OwnedSession
from workspace_fixture import load_target, shell_command


class NamespaceError(RuntimeError):
    pass


def observe(session, cli):
    result = subprocess.run([str(cli), "--session", session.session, "api", "snapshot"], env=session.env,
                            capture_output=True, text=True, check=True, timeout=3)
    value = json.loads(result.stdout)
    state = value["result"]["snapshot"]
    return {"session": session.session, "socket": str(session.socket),
            "workspaces": [row["workspace_id"] for row in state["workspaces"]],
            "tabs": [row["tab_id"] for row in state["tabs"]],
            "panes": [row["pane_id"] for row in state["panes"]]}


def compare(left, right):
    if left["session"] == right["session"] or left["socket"] == right["socket"]:
        raise NamespaceError("owned servers are not distinct")
    overlap = {key: sorted(set(left[key]) & set(right[key])) for key in ("workspaces", "tabs", "panes")}
    if not all(overlap.values()):
        raise NamespaceError("synthetic local IDs did not overlap")
    return {"servers": [left, right], "overlap": overlap, "queued_late_effects": "UNVERIFIED"}


RENDERER = '''import json, subprocess, sys
from pathlib import Path
request, receipt = map(Path, sys.argv[1:3])
value = json.loads(request.read_text())
rows = []
for item in value["servers"]:
 result = subprocess.run([item["cli"], "--session", item["session"], "api", "snapshot"], env=item["env"], capture_output=True, text=True, check=True, timeout=3)
 state = json.loads(result.stdout)["result"]["snapshot"]
 rows.append({"session":item["session"],"socket":item["socket"],"workspaces":[x["workspace_id"] for x in state["workspaces"]],"tabs":[x["tab_id"] for x in state["tabs"]],"panes":[x["pane_id"] for x in state["panes"]]})
emitted = "\\n".join(
    "session={session} socket={socket} workspaces={workspaces} tabs={tabs} panes={panes}".format(**row)
    for row in rows
)
print(emitted, flush=True)
temporary = receipt.with_name(receipt.name + ".tmp")
temporary.write_text(json.dumps({"servers": rows, "emitted": emitted}) + "\\n")
temporary.chmod(0o600)
temporary.replace(receipt)
'''


def stage_renderer(fixture, artifact, sessions):
    root = Path(fixture.fixture_root)
    script, request, receipt = root / "namespace_renderer.py", root / "namespace-request.json", root / "namespace-receipt.json"
    script.write_text(RENDERER); script.chmod(0o700)
    request.write_text(json.dumps({"servers": [{"cli": str(item.herdr), "session": item.session, "socket": str(item.socket), "env": item.env} for item in sessions]}) + "\n")
    request.chmod(0o600)
    fixture.run(shell_command([sys.executable, script, request, receipt]))
    return receipt, request


def await_renderer(receipt, request, artifact, left, right, created, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(receipt).exists():
            value = json.loads(Path(receipt).read_text())
            rows = value['servers']
            observed = compare(rows[0], rows[1])
            if (observed['servers'][0]['session'], observed['servers'][0]['socket']) != (left.session, str(left.socket)) or (observed['servers'][1]['session'], observed['servers'][1]['socket']) != (right.session, str(right.socket)):
                raise NamespaceError('renderer authority differs from owned sessions')
            for name, server in (('first', observed['servers'][0]), ('second', observed['servers'][1])):
                created_workspace = created[name]['workspace']['workspace_id']
                created_pane = created[name]['root_pane']['pane_id']
                if created_workspace not in server['workspaces'] or created_pane not in server['panes']:
                    raise NamespaceError('renderer omitted created server identity')
            final = Path(artifact) / "namespace-receipt.json"
            final.write_text(Path(receipt).read_text())
            (Path(artifact) / "namespace-renderer.json").write_text(json.dumps({"request_sha256": hashlib.sha256(Path(request).read_bytes()).hexdigest(), "receipt_sha256": hashlib.sha256(final.read_bytes()).hexdigest(), "emitted": value['emitted']}) + "\n")
            return observed, value
        time.sleep(.02)
    raise NamespaceError('renderer completion receipt timed out')


def run_lane(repo, revision, artifact_dir, target_file, *, session_factory=OwnedSession, fixture_factory=None, renderer_timeout=3):
    """Run the released live lane; tests inject harmless sessions and fixtures."""
    if fixture_factory is None:
        from workspace_fixture import WorkspaceFixture
        fixture_factory = WorkspaceFixture
    provenance = checked_source(repo, revision)
    target = load_target(target_file)
    artifact = new_artifact(artifact_dir)
    result = {'status': 'FAIL', 'provenance': provenance, 'queued_late_effects': 'UNVERIFIED'}
    try:
      with session_factory(artifact / 'first') as first, session_factory(artifact / 'second') as second:
        def call(session, *parts): return json.loads(session.run(*parts).stdout)['result']
        created = {'first': call(first, 'workspace', 'create', '--cwd', str(first.root), '--label', 'Fixture alpha'), 'second': call(second, 'workspace', 'create', '--cwd', str(second.root), '--label', 'Fixture beta')}
        with fixture_factory(artifact / 'fixture', target) as fixture:
            runtime_receipt, request = stage_renderer(fixture, artifact, (first, second))
            observed, renderer_receipt = await_renderer(runtime_receipt, request, artifact, first, second, created, renderer_timeout)
            grid = fixture.parent_pty_grid()
            capture = fixture.capture('server-identity.png', viewport_grid={'rows': grid['rows'], 'columns': grid['columns']})
            receipt = artifact / 'namespace-receipt.json'
            result.update(receipt=str(receipt), namespace=observed, created=created, capture=capture,
                          renderer_output=renderer_receipt['emitted'],
                          evidence_kind='NATIVE' if fixture.__class__.__name__ == 'WorkspaceFixture' else 'SIMULATED', visual='UNVERIFIED')
      result['status'] = 'PASS'
    except BaseException as error:
      result['error'] = str(error)
    finally:
      (artifact / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--revision', required=True); parser.add_argument('--artifact-dir', required=True); parser.add_argument('--target-file', required=True)
    args = parser.parse_args()
    return 0 if run_lane(HERE.parents[1], args.revision, Path(args.artifact_dir), args.target_file)['status'] == 'PASS' else 2


if __name__ == '__main__':
    from runtime_lease import runtime_lease_owner
    with runtime_lease_owner(repo=HERE.parents[1]):
        raise SystemExit(main())
