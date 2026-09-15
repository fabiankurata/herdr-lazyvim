# Native workspace fixture

`run.py` uses the existing Alacritty client in the approved Herdr workspace. It never starts or restarts Alacritty.

`run.py` requires `--target-file`. The mode-0600 target JSON supplies the exact socket, workspace, development pane, and existing Alacritty identities. There is no default-server or focused-pane fallback. `WorkspaceFixture` records the socket device/inode and owner process/start before creation. Before every later mutation it compares that identity with the current endpoint, checks the configured development identities and Alacritty census, and rejects any replacement. It records the created tab, pane, and terminal IDs, then sends every fixture command to that pane ID.

Before a fixture command, native input, or capture, the fixture tab must contain exactly the recorded root pane. An extra pane blocks the operation and cleanup keeps the runtime directory for recovery.

The save-only lane runs archived candidate Neovim code with an isolated HOME, XDG directories, temporary source file, and `HERDR_BIN_PATH=/usr/bin/false`. Its runtime root is a fresh mode-0700 directory directly under `/tmp`, because Unix-domain socket paths under the evidence tree can exceed `sockaddr_un` capacity. `runtime-root.json` records that root's path, device, inode, mode, and short Neovim socket path. Evidence remains under the requested artifact directory. Cleanup removes the runtime root only when that identity still matches; otherwise it retains it for recovery. Cmd+Enter and Ctrl+S persist local annotations only. It does not call the review delivery command or send an agent message.

Run the guards:

```sh
python3 -m unittest discover -s tests/native -p 'test_*.py' -v
```

After committing a clean candidate, one live lane is allowed:

```sh
python3 tests/native/run.py --revision "$(git rev-parse HEAD)" \
  --target-file /private/path/native-target.json \
  --artifact-dir artifacts/PR00/native-workspace/candidate-1
```

## PR01 comment-operation review

`comment_operations.py` uses the existing approved Alacritty through `WorkspaceFixture`. It never starts or restarts Alacritty, never reads the general clipboard, and replaces `vim.system` only inside the isolated Neovim with a controlled sender that records accepted bytes. The ten lanes call the public comment, edit, list, and send operations. Composer lanes use native typed input and the actual Cmd-Enter or Ctrl-S mapping.

The closed-origin lane keeps the original A composer alive after closing only its normal A source window. It records a separate normal B window before the close and requires its buffer, view, cursor, content, and focus to survive the save unchanged.

Run the native review only after the coordinator grants the runtime slot:

```sh
python3 tests/native/comment_operations.py --revision "$(git rev-parse HEAD)" \
  --target-file ../program/artifacts/program/native-target-shell-viewport.json \
  --artifact-dir "artifacts/PR01/$(git rev-parse HEAD)"
```

The bounded headless smoke drives all ten public fixture phases and their observed
callbacks through an isolated Neovim process. It does not type native keys, take
captures, or establish native PASS. Run it only after the runtime lease gate:

```sh
python3 tests/native/comment_operations.py --headless-smoke --revision "$(git rev-parse HEAD)" \
  --artifact-dir "artifacts/PR01/$(git rev-parse HEAD)/headless-smoke"
```

Headless Neovim starts with a fresh HOME, XDG config/data/state/cache roots, and
the `herdr-pr01-review` app name before it resolves `stdpath()`. The fixture
seeds the exact resolved state directory that the product reads. Its artifact
contains a bounded command log, a report, and persisted diagnostics for every
completed lane, including the failing lane when Neovim exits unsuccessfully.

The native runner writes its preflight receipt before it validates the private target or calibration, so unavailable prerequisites remain durably `UNVERIFIED` before any GUI action. It records a result for every lane in `review/`, creates the ten named calibrated PNGs, and records a separate 30 to 60 second review interaction through fresh calibrated captures to build `review/review.mp4`. Each frame has its own capture receipt and the video uses measured monotonic intervals, rather than a frame count or synthesized slideshow cadence. It records `FAIL` only after an observed lane or guarded fixture step fails.

The runner encodes raw Neovim key and typed bytes as ASCII hexadecimal JSON. A screenshot needs a private `capture_calibration` in the mode-0600 target file. The calibration records exact AX bounds, one CoreGraphics window ID, PNG scale, the one-pane fixture layout, the observed parent PTY grid, and an inward crop relative to that window. The pane rectangle and PTY grid are separate geometry domains: a normal-screen terminal may reserve a column, so the grid may be smaller than the pane but may never exceed it. The private target schema calls the grid `viewport_grid`; coordinators must replace the earlier `nvim_grid` field before a checkpoint run. Before capture, all recorded client, layout, and observed-grid values must match exactly. The runner then calls `screencapture -R` only for the calibrated screen rectangle; it never saves a full-window or desktop image or launches an Alacritty process. Its receipt records the rectangle, expected PNG pixels, and all bindings. A missing, malformed, or mismatched calibration leaves the screenshot UNVERIFIED. A valid PNG with the calibrated dimensions marks the runner screenshot step PASS; visual review remains required. A missing screenshot or any recorded lane failure exits with code 2.

The calibration is deliberately one-frame-specific: it never adapts to a changed window, CoreGraphics window, layout, or parent viewport grid. Calibrate the pane rectangle and PTY grid independently from one observed frame, then inspect that frame's saved PNG before reuse. Client chrome or font changes that leave every recorded binding unchanged are outside the available Herdr and Accessibility metadata; do not reuse the calibration without a new verified pixel inspection. A live run must inspect the saved PNG before treating fixture content as verified.

Checkpoint capture first writes `<name>.context.json` beside the per-checkpoint directory, before creating a fixture or focusing either server. It contains only serializable requested target fields, the observed viewport baseline, and expected raw/canonical viewer executable paths. Capture then displays the owned isolated Herdr session through a short-lived relay in the fixture's existing shell. The viewer starts through `/usr/bin/env -i` with only the isolated HOME, PATH, XDG roots, TERM, and owned `HERDR_SOCKET_PATH`; it cannot inherit parent Herdr nesting or configuration. The caller supplies a unique `PR00CAP-` marker and the adapter temporarily assigns it to the validated owned isolated tab, restoring the exact original label during cleanup. The relay accepts only that complete marker in visible terminal text after excluding CSI, OSC, and control payloads. This proves terminal relay readiness, not PNG content.

It stages both the relay and its transcript in the already-owned short fixture root before invocation, then preserves the finished transcript and final relay receipt, including `child_exit`, in the artifact directory before teardown. This keeps the terminal command below the PTY line limit even when this checkout or the artifact directory has a long path. The relay's atomic early receipt records child and relay PID/start ownership plus raw `ps comm` diagnostics and a macOS `proc_pidpath` kernel image observation; `viewer-early.json` preserves that evidence outside the disposable runtime. A relative command name is retained as a diagnostic and is never resolved as an executable path. After the render marker, the runner samples the child again, requires the same PID/start tuple, and compares the canonical kernel image path with the requested viewer path. Kernel-query failures and raw values are persisted before these guards reject a run. Cleanup uses the most recently verified PID/start identity, rejects a reused PID, and retains uncertain resources. It stops the child and relay before restoring the isolated viewport. The parent fixture shell is never replaced with `exec`, so it remains alive until explicit fixture teardown.

On success, teardown restores the prior tab only if the fixture remains focused, closes only the recorded tab, verifies that the tab disappeared, and compares the socket owner and Alacritty census with the pre-create values. If an unknown pane appears in the fixture tab or a cleanup check fails, it leaves the runtime directory and records the cleanup error.
