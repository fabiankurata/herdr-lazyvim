# Live panes with bounded resize recovery and a Rust controller

Select live-pane persistence with an explicitly owned placeholder for sole-editor hide and a measured resize workaround scoped to Herdr 0.9.0. Use Rust for the external controller and Lua for the annotation package. Independent matched measurements and native experiments support the prototype behavior and cleanup claims. Production recovery belongs to PR05, distribution and supported-platform evidence to PR09, and the final PR00 acceptance verdict to the independent frozen-candidate review.

The prototype branch starts at reviewed baseline `488f4c543ed7ba0937b8a8725ef438b79112c800`. The experiment source and repeatable commands are in [tests/prototypes](../../tests/prototypes/README.md). The independently tested candidate is `f5d9222f28495473689c86b36158762c8da2e887`, tree `9cf24c5aeed88e452f4fd3a2acdfa62384973b3b`. Evidence references use the relative convention `artifacts/PR00/<tested-sha>/...`; no runtime file changes in this slice.

## Existing controller behavior

The manifest runs `herdr/pane.sh`, which reads configuration with regular expressions, takes launch context or inherited pane environment variables, lists the workspace panes, and runs `pane process-info` for each pane. It identifies editors by a shared argv marker and chooses the focused match or the first match.

For reuse, toggle hides only when the editor itself has focus. A visible editor beside a focused terminal receives a focus action. A single-pane tab outside the invoking tab counts as a parked editor. Hide refuses when the editor is its tab's sole pane. Otherwise it leaves zoom, moves the editor into a new tab, and uses a fixed delay plus zoom commands to resize the remaining terminal. Restore moves the editor beside the selected target and may use fixed-delay resize nudges. The move response updates the local pane ID, but the script writes no durable session or invocation record.

`herdr/launch.sh` assembles configuration into Vim and Lua command strings and starts Neovim. `herdr/config.sh` handles flat strings and booleans without a TOML parser. The existing shell tests assert calls against fixtures and do not establish live PTY preservation or geometry convergence.

The source exposes the following risks:

- A marker match is not a verified process/session identity. Deduplication closes other matching editors, which may contain unsaved text.
- Focus-based toggling requires a second action when the visible editor is unfocused. Pane count cannot distinguish an intentional editor tab from a parking tab.
- Explicit server identity, process-safe session locks, topology locks, and durable effect progress are absent. A timed-out move can commit after the client stops waiting.
- Fixed sleeps and successful resize commands do not observe the Herdr content rectangle, PTY, and Neovim grid together.
- Target fallback can select a pane outside the invoking tab. Launch environment can become stale after transfer.

## Installed protocol observations

The inspected host has Herdr 0.9.0, bundled protocol 22 and schema version 1, Neovim 0.12.5, Rust 1.98.1, and Alacritty 0.15.1. `herdr api schema --json` is a bundled schema read; it does not establish live server conformance.

`SessionSnapshot` includes topology, agent records, version, and protocol, but no durable server boot identity. `PaneProcessInfoProcess` reports PID, argv, name, and cwd without process start time. Connection namespace and an independently verified editor token/process handshake are therefore necessary for safe adoption. Pane IDs remain opaque and server-specific. Moves must use the returned pane ID.

`AgentPromptParams` has `target`, `text`, and `wait`. `AgentSendKeysParams` has `target` and `keys`. `PaneSendTextParams` lacks an expected occupant/session condition. No documented atomic expected-session input parameter was found. Strict guarded delivery must remain unsupported until live evidence proves a suitable capability. This observation does not establish the timing of occupant resolution inside the server.

The initial macOS automation probe reported UI automation disabled; that was later enabled. The f5 fixture-only native lane subsequently passed its scoped Cmd-key, save, and calibrated-capture checks. It is not evidence for the scenario-specific screenshots and presentation required across all ten lanes.

## Runtime comparison and recovery contract

The Rust and headless Lua sketches implement the same small action protocol. Both preserve an unresolved invocation as `observe` with retained ownership. Both complete an independently confirmed invocation without replaying its toggle. Both refuse mutations for ambiguous ownership. The test process runs each request through a fresh controller process and validates literal outcomes, including invalid requests, lost-response observations, and confirmed completion.

An earlier planner-startup microbenchmark recorded Rust p50 3.82 ms and p95 4.30 ms, and Lua p50 16.43 ms and p95 17.03 ms. It included subprocess startup but excluded editor launch, Herdr calls, TOML parsing, durable storage, locking, packaging, and network effects. It is historical probe evidence only and is not comparable to the matched live measurements below or a production lifecycle budget.

The independent f5 gate ran serialized, sample-by-sample AB/BA comparisons: 30 baseline and 30 candidate samples for legacy, Rust, and Lua. All 180 samples completed with required behavior and cleanup evidence; all 180 teardown receipts passed. The source archives were exact baseline and candidate revisions. The resource readings cover steady server and client process trees with fixture roles, exclude workers and short-lived controllers/observers, and do not claim peak RSS. Geometry used one deadline from immediately before each action and did not reset it during recovery.

For the candidate, legacy p95 cold readiness/hide/show was 606.64/768.23/191.05 ms; Rust was 957.44/59.32/33.86 ms; Lua was 634.15/69.17/41.48 ms. Legacy warm hide exceeded the provisional 500 ms budget for both baseline (783.28 ms p95) and candidate (768.23 ms p95); its show passed in both arms. Rust and Lua hide/show met that provisional budget in both arms. These are like-for-like measurements within each runtime and instrumented comparison, not a cross-runtime speedup claim.

Before PR04, revise the instrumented legacy reference warm-toggle budget to 800 ms p95 on this host/profile. This is the next 100 ms boundary above the higher measured legacy hide p95 of 783.28 ms. Preserve the original 500 ms failure in both arms. The selected Rust controller and Lua comparison retain the 500 ms warm-toggle target, every geometry deadline remains 1 s, and no budget permits lost buffers or wrong-session mutations. The revised legacy reference budget applies to subsequent validation; it is not a production-controller target.

## Backend decision gate

The standalone remote-UI run passed 100 attach/resize/detach cycles with dirty text entered through terminal input. The editor and local fixture LSP process retained their PIDs. PTY dimensions and the Neovim grid matched on every cycle. This run used plain Neovim outside Herdr.

The owned live Herdr run also passed 100 park/restore/transfer cycles. It retained the dirty editor, its local fixture LSP, and three `cat` processes in two tabs with a nested split. Zoom transitions and client resizes were included. Each observed content rectangle matched the independently read PTY dimensions and Neovim grid. The initial and final tab counts were both two. Full cycle time was 108.05 ms p50 and 120.60 ms p95, including diagnostic subprocesses. This is a direct API experiment, not either planner executing production lifecycle logic.

A later 100-cycle rerun with raw-input fixtures failed at zero-based cycle 20. The split content measured 37 rows by 55 columns, while the actual editor PTY and Neovim grid remained at 39 by 114 beyond one second. Dirty text and the LSP survived. That counterexample overrides the earlier passing run for backend acceptance. The retained trace records the move, client resize, layout, PTY, and editor observations. A Herdr 0.9.0-specific nudge is an experiment until it demonstrates convergence under one unchanged overall deadline.

The corrected historical experiment at `755f99f63a6754a6e9d1e93fa0a3556a1df603fb` passed 100 cycles with four observed mismatches that required one left/right resize round trip. Every content/PTY/grid match arrived before the original one-second deadline, which starts before the final move invocation and is never reset for the nudge. Geometry time was 192.02 ms p50, 209.48 ms p95, and 394.34 ms maximum. The full park/restore cycle, including diagnostic processes, was 390.90 ms p95. Exact fixture input, editor/LSP retention, both interruption cases, and the sole-workspace placeholder passed in that run. Guarded teardown confirmed unchanged protected identities.

The workaround is scoped to Herdr 0.9.0 and runs only after an observed mismatch. Successful resize commands alone do not establish success. Remove it after the same supported-version workload passes without it. This result establishes a bounded recovery path for the observed failure, not a fix to Herdr itself. Retained PR00 evidence records the exact helper source for the historical run.

Moving the editor from a sole-pane parking tab succeeded and returned a new parking tab plus `closed_tab_id` for the emptied tab. Herdr therefore supports this move without closing the editor process.

A subsequent experiment moved the editor into a workspace where it was the only pane. Creating one explicitly owned terminal placeholder allowed the editor to hide in a separate owned parking tab and restore beside the same placeholder. Dirty text, editor PID, and the LSP survived. This is the proposed sole-editor policy: retain the owned placeholder on restoration and never close a user terminal to hide the editor. The installed controller is unchanged.

Both sketch interruption experiments also passed. A test driver invoked the Rust or Lua planner, executed its chosen Herdr move, and was killed after the successful response but before acknowledgement. Restarting the planner with the captured uncertain invocation returned observation only with retained ownership. The editor and LSP survived, and no fixture terminal was deleted. This records the ambiguous recovery state required by PR00; it does not establish PR05's durable recovery implementation.

The fixture terminals now capture raw input bytes. Known markers sent before and after transfers, and again after interrupted operations, arrived exactly once at each selected fixture process. The result includes independent captured and expected hexadecimal payloads. Earlier 100-cycle timings used `cat` terminals, so the final captured-input run has its own receipt.

Two historical probe errors were corrected before these results. The remote-UI driver must drain output while waiting for UI exit. The Herdr geometry observer must use the whole layout area when the editor is zoomed. Retained PR00 evidence preserves the failed probe runs and corrected reruns.

The later exact candidate `0c9896f0559667a2cad640fd42a46bae1ca63ef6`, tree `0dd5af3cc05c7598163f8c919e5bcf27b769ca8f`, passed the required unit suite and one native batch with six independently inspected screenshots: initial visibility, transfer geometry, both sketch interruptions, sole-editor hide, and restoration. Its owned viewer retained the dirty editor, LSP and exact fake-agent input, restored the original viewport, and cleaned up without changing default-session identities. Evidence is `artifacts/PR00/0c9896f0559667a2cad640fd42a46bae1ca63ef6/native-checkpoints-extras/verifier-result.json`. It supports lanes 2, 3, 5, 6, 7 and 8; it is separate from the 100-cycle and matched performance runs.

The remaining native experiments subsequently passed independent review. Exact `5edfe21bb31db166ed7bb0c7332bae02457f7ff7` presented two fresh named-server authorities with overlapping local IDs, inspected the fixture-only image, and verified both teardowns. Exact harness `9ee4204b3450311f1a4ab1c6412e79c271787a25` ran the same legacy hide/show experiment against baseline `488f4c543ed7ba0937b8a8725ef438b79112c800` and its candidate. Both arms passed their initial/transfer screenshots, dirty editor/LSP retention and cleanup. The harness booted the editor directly; the separate matched comparison measures the subject launcher path.

Exact `899f2472b85eb9060f20f281701a24837e859d72`, tree `ea324a922d3a3ff331ce925f1209183b8f40b107`, passed the required unit suite and native Plain and LazyVim remote-UI experiments. Each passed actual clipboard paste/yank equality, clipboard restoration, CmdEnter/CtrlS annotation persistence, inspected fixture-only capture, dirty editor/LSP retention after detach and owned cleanup. Saves used disabled real delivery. LazyVim used exact locally archived dependencies without downloads. No tested remote-UI case established an advantage over the passing live-pane experiment.

Clipboard comparisons return booleans from inside Neovim. Raw transcript recording stops before clipboard input and remains off until the UI driver exits, excluding unexpected clipboard content and delayed output. Safe key, comparison, restoration and cleanup receipts plus the successful post-restoration screenshot cover that interval; raw-output coverage there is partial. Full default presentation state is UNVERIFIED because terminal titles and scroll fields can change; stable workspace/tab/pane/terminal/agent-session, socket and Alacritty identities passed. The earlier LazyVim readiness failure remains preserved with an unknown cause; the exact899 full suite and both native profiles passed.

These native results are representative cycles, separate from the original 100-cycle experiments and 180 matched measurements. Each receipt retains its tested SHA. The final verifier checks source equivalence and the frozen candidate before combining evidence. Per-lane receipts are under `artifacts/PR00/<tested-sha>/`; final performance and acceptance are recorded under the frozen PR00 candidate directory.

| Lane | Independently reviewed evidence | Scope |
| --- | --- | --- |

| 1 | Exact9ee harness, baseline488f and candidate9ee legacy arms, four inspected captures and cleanup | Same warm hide/show workload; subject cold launch is measured separately |
| 2 | Exact0c named boot/shutdown, native capture and protected identities | Owned session and approved existing client |
| 3 | Dirty editor/LSP retention and exact0c native transfer capture | Live pane transfer |
| 4 | Original 100 remote-UI cycles; exact899 Plain/Lazy native attach/detach, input, clipboard and focus | Native representative cycle per profile; raw clipboard-phase transcript omitted |
| 5 | Owned placeholder and exact0c hide/restore captures | Sole-editor policy retains the owned placeholder |
| 6 | Nested fake processes, exact bytes and exact0c captures | Synthetic fake agents only |
| 7 | Content, PTY/Neovim grid and exact0c native geometry capture | Version-scoped recovery under one deadline |
| 8 | Both interrupted sketches preserve uncertainty/processes; exact0c captures | Prototype recovery, not PR05 durable execution |
| 9 | Exact5ed native authority presentation and both named-server teardowns | Distinct sockets with intentionally overlapping local IDs |
| 10 | Exact899 Plain/Lazy startup, native shortcuts, clipboard and inspected captures | Pinned local archives; unavailable optional integrations remain UNVERIFIED |

A guarded local transport queue held a `pane.move` request until its caller timed out, then forwarded it to the owned Herdr socket. Herdr returned a successful move after the timeout, and the observed pane location changed. The second named server remained unchanged. This proves that this queued request can commit after caller timeout. Herdr's internal scheduling and cancellation behavior remain UNVERIFIED. The pure planners' observation-only result does not establish fencing, durable locks, or executor correctness. The completed serialized baseline/candidate measurement remains limited to the prototype behavior and cleanup scope stated above.

The initial real legacy warm probe ran 30 hide/restore pairs against baseline `488f4c543ed7ba0937b8a8725ef438b79112c800`. Every pair preserved the dirty editor and LSP with converged geometry. Pair latency was 476.71 ms p50 and 510.31 ms p95. This is two actions and must not be compared with the single-toggle budget. That run preceded per-action call-count instrumentation. The repeatable workload now records separate show/hide durations, external call counts, and owned process RSS. The counter wrapper adds subprocess overhead to both comparison arms.

Live-pane persistence retains editor state and supports the placeholder and interruption experiments. The version-scoped remedy passed the reproduced resize case within one overall deadline. PR05 must turn these observations into an operation journal, process-safe ownership, geometry observation, and recovery implementation, including preservation of later user-created splits.

Remote UI remains an experimental alternative. Its retention, native input, clipboard, focus and LazyVim experiments passed within the stated scopes. It has not passed a case live-pane persistence cannot, so it does not replace the chosen backend. The independent matched measurement is complete. Use the revised 800 ms legacy reference budget, the unchanged 500 ms Rust/Lua warm-toggle target, and the 1 s geometry deadline above. None establishes production behavior.

Both runtimes satisfy the same prototype action contract and preserve uncertain progress in the interruption experiment. In the current matched live measurements, Lua reached cold editor readiness faster, while Rust had lower candidate warm hide/show p95. That tradeoff, together with the existing Rust-oriented controller plan, supports selecting Rust for the planned typed controller boundary. It does not establish a cross-runtime speedup or a packaging result. Production dependency size and platform artifacts remain PR09 evidence. PR00 records interruption and ambiguity; production fencing, locking, and automatic recovery belong to PR05. This design choice does not waive the frozen-candidate PR00 performance and acceptance gates.
