# PR00 controller experiments

These programs are experiments. They do not install or replace the controller.

`controller.rs` and `controller.lua` accept the same four arguments, in order:

| Argument | Values |
| --- | --- |
| Requested action | `show`, `hide`, `toggle` |
| Observed editor location | `absent`, `hidden`, `here`, `elsewhere`, `ambiguous` |
| Captured invocation progress | `idle`, `uncertain_show`, `uncertain_hide`, `confirmed_show`, `confirmed_hide` |
| Editor ownership | `verified`, `unverified` |

The result is JSON with `effect`, `outcome`, and `retain_guard`. Invalid arguments return an `error` code and exit 2. `here` refers to the captured invoking tab, regardless of keyboard focus. The caller must supply a separately resolved SessionKey. These sketches do not resolve context themselves.

The correctness condition is that an unresolved mutation cannot admit another topology mutation. Both sketches return `observe` and retain the guard for uncertain progress, even if a snapshot already shows the desired layout. A matching, independently confirmed invocation receipt permits completion without toggling again. Ambiguous or unverified editor ownership permits no mutation. Repeated explicit show and hide requests already at their destination need no effect.

These are pure planners. They have no Herdr adapter, durable journal, lock implementation, process handshake, cancellation, or effect executor. Guard fields describe the required executor behavior. The comparison does not prove that an executor follows it. The fault sequence starts fresh controller processes, but supplies captured progress from the test process. It does not prove durable recovery after an operating-system crash.

Run the comparison with installed Rust and Neovim:

```sh
python3 tests/prototypes/compare.py --artifact-dir /tmp/pr00-comparison --samples 30
```

The runner compiles the dependency-free Rust sketch outside the source tree and starts Lua through `nvim --headless -u NONE -i NONE`. It validates 19 cases per runtime, then alternates runtime order for process-start measurements. `comparison.json` contains the individual durations and p50/p95 summaries. This is planner startup cost. It is not warm editor visibility latency or a comparison against the legacy shell controller.

Run the standalone remote UI experiment:

```sh
python3 tests/prototypes/remote_ui.py --artifact-dir /tmp/pr00-remote-ui --cycles 100
```

The runner creates an isolated plain Neovim profile, a headless editor, a real local fixture LSP process, and one disposable PTY UI at a time. It enters text through terminal input, resizes the PTY, sends `:detach`, and checks that the same editor and LSP retain the dirty text. It compares PTY rows/columns with the Neovim grid after every resize. It drains terminal output while waiting for UI exit. The runner stops only the processes it starts.

`result.json` records every cycle and lists missing capabilities. `terminal.bin` contains raw terminal output and `server.log` contains startup diagnostics. No screenshot, native Cmd-key input, clipboard, LazyVim startup, Herdr content rectangle, or Herdr pane transfer is established by this program. The fake LSP only speaks the initialization and text synchronization protocol needed to check process retention.

Keep outputs outside tracked source. Use fresh artifact directories for independent runs so earlier failures remain reviewable.

The macOS live-pane experiment uses the guarded named-session context from `tests/live/owned_session.py`:

```sh
python3 tests/prototypes/live_pane.py --artifact-dir /tmp/pr00-live-pane --cycles 100
```

When `--native-capture-hook` is enabled, the owned runtime exclusively creates `$XDG_CONFIG_HOME/herdr/config.toml` with `onboarding = false` before its original client starts. The artifact records its path and SHA-256. Non-native runs do not create that onboarding control. Matched performance runs separately create their owned update control with version and manifest checks disabled before any Herdr command.

It parks and transfers a dirty editor between two tabs containing three fixture input-capture processes, including a nested split. It tests zoom transitions, moves a sole-pane parking tab, resizes a PTY Herdr client, and reads the editor PTY size through its actual stdin descriptor. The Herdr 0.9.0 layout rectangle includes a one-cell border. For a zoomed focused editor, the full layout area supplies its rectangle. Geometry succeeds only when that content size, the PTY, and the Neovim grid agree within a one-second observation window. Each fixture captures known input bytes before and after the moves.

`--legacy-checkout /path/to/checkout` runs that checkout's real `herdr/pane.sh` for each hide and restore. An explicit named-session wrapper keeps its commands in the owned fixture. The editor uses the legacy argv marker and an isolated plain profile. This measures warm legacy behavior with a manually prepared editor; it does not exercise legacy plugin installation or its cold launcher. The direct prototype and legacy mode have different placement policies, so their latency values do not establish a speedup. `--runner-dir` selects the directory containing `owned_session.py` when the owners' worktrees have not yet been integrated.

`source-hashes.json` records the exact experiment and session-context sources. `trace.json` contains Herdr responses, and `result.json` contains identities, dimensions, timings, and initial/final tab lists. Screenshots and native keyboard proof remain separate work.

For a matched legacy comparison, `--checkout` aliases `--legacy-checkout` and `--revision` checks the target checkout's HEAD. `cycles[].hide` and `cycles[].show` contain separate elapsed times and external CLI counts. The count wrapper adds Python process overhead to every call, so use the same instrumentation in both arms. The before/after resource fields report PID and RSS in KiB for the owned editor, LSP, and fixture terminals. `fixture_editor_ready_ms` measures the explicit fixture editor launch, not the installed legacy launcher.

Run the server namespace and queued-timeout probe with the same guarded context:

```sh
python3 tests/prototypes/server_probe.py --artifact-dir /tmp/pr00-server-probe
```

It starts two named servers, records their overlapping pane/workspace IDs, and forwards one queued move after the caller times out. The result records the server response and the unchanged second server. The queue models a delayed transport. It does not establish Herdr's internal cancellation semantics or a controller's recovery implementation.

`live_pane.py --extra-scenarios` also kills a test driver after each planner's selected move succeeds, restarts that planner with the captured uncertain invocation, and records its observation-only result. It checks that all fixture terminals and dirty editor state survive. A second experiment moves the editor to a workspace where it is the only pane, creates one owned terminal placeholder, parks the editor, and restores it beside that placeholder. The resulting policy keeps the placeholder on restore. These are experimental policies; no installed controller behavior changes.

`--resize-recovery nudge` is scoped to Herdr 0.9.0. After observing a content/PTY/grid mismatch, it performs one left/right resize round trip. It never resets the one-second deadline that starts at the show/move invocation, and success still requires an observed match before that deadline. The default `observe` mode sends no nudge. Remove this workaround only after the same resize/transfer workload passes without it on the supported Herdr version. It does not establish that Herdr's resize defect is fixed.

The matched live bridge exercises archived `launch.sh` and `pane.sh` through the shared comparator:

```sh
python3 tests/prototypes/compare_live.py --harness-root /path/to/assembled/checkout \
  --baseline <exact-sha> --candidate <exact-sha> --samples 30 \
  --runtime legacy --artifact-dir /tmp/pr00-matched-legacy
```

The comparator extracts both revisions and alternates AB, BA at sample granularity. Neither archive needs Git metadata or a test runner. Every sample uses the comparator's existing owned named server. The callback supervises one worker and its descendants through `OwnedSession.execute`; it does not create another server. Archive receipts and runtime file hashes identify the measured code. Instrumentation hashes identify the worker, fixture helpers, comparator, and session owner independently.

Cold readiness starts before the real `pane.run` call that executes the archived launcher. It ends when Neovim reaches VimEnter, opens the expected fixture, sets the legacy plugin marker, and registers the review command. The isolated profile enables the archived review plugin and uses a plain Neovim fixture init. Registry opening, downloads, Rust compilation, server/client startup, fixture preparation, and subsequent LSP readiness verification are outside that metric. This is an actual launcher measurement, with that limited profile scope.

Warm hide and show each report command duration and duration through observed placement and geometry readiness. The combined metric is their sum. The legacy call-count wrapper starts Python once per Herdr call, appends argv, then execs Herdr in the same PID; that interpreter startup and append are inside command timing. Each independent observer launches `herdr pane get`, `herdr pane layout`, and, for the visible editor, `nvim --remote-expr`; these launches are included in readiness duration but excluded from controller call count. Any experimental recovery mutation is counted separately and included in the total. `subprocesses.json` records direct process launches by phase; it does not claim to enumerate shell-internal subprocesses. The geometry deadline starts before the action and is never reset by observation or recovery.

`--runtime rust` and `--runtime lua` execute the selected planner process inside each cold/warm interval, then apply its asserted action through a common Python effect executor. Both use the same archived launcher and zoomed placement as the legacy sample. These are real planner/executor compositions, not production controllers. They include a targeted observation before each warm decision and one Herdr 0.9.0 resize nudge only after an observed editor mismatch. Compare each mode's absolute measurements and behavior; the legacy wrapper has additional instrumentation overhead, so the report makes no cross-runtime speedup claim.

After hiding the editor, the remaining owned agent pane can retain stale PTY dimensions. In Rust/Lua composition only, a single observed mismatch on Herdr 0.9.0 triggers one agent zoom-on/zoom-off roundtrip. The two mutation calls and their duration are recorded, the agent must finish unzoomed, and the original action deadline remains in force. This mirrors the legacy hide path; it is not a Lua planner-specific behavior.

Steady process count and summed RSS cover the owned server and Herdr client descendant trees, including remaining shells, after both actions. Identified roles include editor, fixture LSP, fake agent, server, and client. These metrics exclude the measuring worker and short-lived controller/observer processes and do not measure peak RSS. Dirty contents, unchanged saved bytes, editor/LSP identity, fake-agent process retention, and exact synthetic input bytes must all survive the sample. On a failure after setup, `failure_state` records bounded read-only process-info, editor dirty/text/LSP, fake-input, and parent default-identity observations before teardown. A missing or failed observation is marked unavailable and cannot replace the original error or geometry trace. Failed samples and geometry traces remain in the evidence.

Thirty samples per arm are required for the bridge's measurement PASS. Smaller smoke runs remain UNVERIFIED. The provisional 500 ms warm p95 budget is reported separately for hide/show and is not relaxed when the legacy controller exceeds it. A measurement PASS does not establish native Cmd behavior or complete PR00 acceptance.

Native checkpoints use `--native-capture-hook tests/native/checkpoint.py --native-target-file /private/path/native-target.json`. The outer fixture always uses that approved default connection. For `live_pane.py`, the nested Herdr viewer receives the separately owned isolated session explicitly. The hook records both identities, starts the viewer only in the recorded outer fixture pane, binds the crop to that parent viewport grid, and stops the recorded viewer before the outer fixture closes. This requires the private calibration schema's `viewport_grid` field. Completed native-checkpoint receipts support that recorded fixture/viewer capture scope; they do not establish Remote-UI, profile, server-probe, or boot-isolation checkpoint behavior.
