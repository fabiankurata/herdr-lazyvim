#!/usr/bin/env python3
"""Run regression commands through the same owned runtime supervisor."""
import os
from pathlib import Path
import sys
import time

from owned_session import OwnedSession
from runtime_lease import runtime_lease_owner
from source import new_artifact


def main():
    harness = Path(__file__).resolve().parents[2]
    artifact = new_artifact(os.environ.get('HERDR_TEST_ARTIFACT_DIR') or
                            harness / 'artifacts/PR00/harness-deep' / ('tests-' + str(time.time_ns())))
    with runtime_lease_owner(repo=harness), OwnedSession(artifact, real=False) as runtime:
        state = Path(runtime.env['XDG_STATE_HOME'])
        env = runtime.env | {'HERDR_TEST_STATE_ROOT': str(state)}
        # Make needs its repository cwd; pass -C explicitly instead of exposing
        # arbitrary project configuration to the owned runtime's cwd.
        command = sys.argv[1:]
        if command and Path(command[0]).name in {'make', 'gmake'}:
            command = [command[0], '-C', str(harness), *command[1:]]
        result = runtime.execute(command, env=env, check=False, timeout=120)
        (artifact / 'stdout.txt').write_text(result.stdout)
        (artifact / 'stderr.txt').write_text(result.stderr)
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
