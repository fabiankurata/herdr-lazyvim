# Feedback contract version 1

These contracts define the implementation boundary for PR01 through PR10.
PR00 keeps the installed runtime unchanged. Examples use synthetic identities.
PR00 finalizes the plan's proposed identity shapes by selecting socket-based
connection identity. Named sessions resolve to an explicit socket before any
operation; a namespace-only connection is not accepted by version 1. The
short-XDG named-session probe demonstrated this resolution on Herdr 0.9.0.
This choice keeps one routing authority for runtime calls and ownership checks.
It does not require a durable server boot ID.

The Lua API version, controller protocol version, and storage schema version
are independent. Each starts at 1. Changing one does not rename the store.

## Identity

`context-v1.json` separates connection, observed server incarnation, immutable
session scope, attachment, and review identity. A connection contains host
and path authority plus an explicit normalized socket. A named server is a
label for that socket, never a substitute for explicit connection routing.

A workspace SessionKey has no tab ID. A tab SessionKey requires a tab ID.
Moving an editor changes its EditorView and refreshed handshake, not its
SessionKey. Workspace labels never participate in keys. Server observations
record only facts exposed by the server or an independently checked process
handshake. A process ID alone cannot establish ownership after restart.
`editor_identity` records the token, verified handshake, PID, and independently
observed process-start identity. Unknown fields remain null. Adoption is
refused until the token and process incarnation are verified.

A WorktreeKey uses the canonical checkout root and host/path authority.
Linked worktrees have distinct roots even when they share a Git common-dir.
A non-Git directory uses its canonical root. A ReviewKey adds the review ID,
initially `draft`. Serialize complete structured keys canonically, then hash
with SHA-256. Store the complete key alongside records and reject a hash/key
mismatch. Do not truncate digests or split identifiers at punctuation.

## Source and annotation

`annotation-v1.json` contains an immutable complete-line source capture.
Line numbers are one-based and inclusive. Characterwise and blockwise Visual
selections capture all touched lines in version 1. They do not promise exact
column capture. The live anchor is disposable editor state and never rewrites
the original source range or source lines.

`content_sha256` hashes the UTF-8 captured lines joined by newline, including
a final newline. It identifies the captured text, not the full working file.
Working-tree captures contain this hash. Index and revision captures
also contain immutable Git blob identity and a commit where one exists.
The source kind is `working_tree`, `index`, `revision`, or `snapshot`.
An unresolved live location opens a labeled snapshot. A buffer number never
identifies a persisted source. `contracts.lua` defines transient origin state
separately from serialized source state.

Each annotation has a UUID, positive integer revision, ReviewKey, source,
comment text, and timestamps. Distinct annotation records use distinct paths.
An update or deletion requires the expected revision under an exclusive
process-safe per-record commit. A stale update returns `revision_conflict`
with the local text preserved. Atomic rename alone is insufficient.

Deletion creates a revisioned tombstone. Keep tombstones indefinitely in
version 1. No cleanup or stale writer can resurrect a deleted ID. A future
cleanup policy requires separate concurrent-reader evidence.

## Delivery and completion

`delivery-v1.json` fixes batch membership and source payload before target
selection. `send` uses the bundled Herdr transport unless its optional
`transport` is a nonempty registered name. A selected transport receives the
immutable exported batch and can supply its own target shape. Selected Herdr
targets contain the explicit connection, workspace/tab, pane, and observed
agent session identity. The Herdr transport validates the occupant immediately
before input delivery. If server-side conditional session delivery is
unavailable, `strict_session_guard=true` fails with `unsupported_capability`
before sending bytes. The default preflight check has a remaining race before
the server accepts input.

All public operations that perform I/O accept a final `done(result)` callback
and return an operation handle with `cancel()`. Completion is scheduled on
the Neovim main loop exactly once, including validation errors. Registration
and setup are synchronous and return a structured result. A result has either
`ok=true,value=...` or `ok=false,error={code,message,context,...}`. Callbacks do
not use thrown errors or message parsing as their protocol.

Capture the ReviewKey, annotation revisions, source text, and OriginContext
before any composer, picker, or asynchronous call. `cancel()` requests local
cancellation. It cannot retract accepted bytes or roll back a committed write.
Before an external mutation, cancellation completes with `cancelled`. After
an unconfirmed mutation it completes with `uncertain`, retains drafts, and
never retries automatically. A confirmed mutation returns its committed
result even if cancellation races with completion.

Delivery outcomes are `cancelled`, `failed`, `delivered_to_input`, or
`uncertain`. They do not assert that an agent executed the feedback. A focus
failure after delivery is a warning on the delivered result. It does not undo
the receipt or clear other drafts.

`receipt-v1.json` records acknowledged batch revisions durably before clearing
matching drafts. Replay compares both ID and revision and is idempotent.
A newer annotation revision remains pending. Receipts occupy separate records;
they never rewrite a global annotation index.

## Public API

The examples below fix the call shapes. They are targets for the later
package extraction and are not available in the PR00 baseline runtime.

```lua
local feedback = require("herdr_feedback")
local configured = feedback.setup({ keymaps = false })
feedback.add({ bufnr = 0, range = { start_line = 2, end_line = 4 }, text = "Explain this branch" }, done)
feedback.edit(id, { review = review_key, text = "Add a test", expected_revision = 1 }, done)
feedback.delete(id, { review = review_key, expected_revision = 2 }, done)
feedback.list({ review = review_key }, done)
feedback.export({ review = review_key, annotation_ids = { id } }, done)
local operation = feedback.send({ review = review_key, annotation_ids = { id }, target = target, submit = false }, done)
operation.cancel()
feedback.register_source_adapter("example", adapter)
feedback.register_transport("capture", transport)
feedback.send({ review = review_key, annotation_ids = { id }, transport = "capture" }, done)
```

`add` resolves the buffer's review once at invocation. Calls using existing
IDs require an explicit ReviewKey. An external caller cannot accidentally
select a different root because the editor's current buffer changes.
`list` returns records, `export` returns an immutable batch payload, and `send`
returns a delivery outcome plus batch ID and any warning.

## Extension contracts

A SourceAdapter implements `resolve(request, done)`, `capture(request, done)`,
and `navigate(request, done)`, each returning an operation handle. Resolve
receives a transient buffer and explicit host/path authority. Its successful
value is a SourceLocation. An unsupported buffer returns `not_applicable`.
Capture receives that SourceLocation and a complete-line range, and returns
an immutable SourceCapture. Navigate receives a SourceCapture and returns a
live location or labeled snapshot. Results are validated at the adapter
boundary. The ordinary-file adapter remains usable if Diffview fails.

A Transport implements `list_targets(request, done)`,
`validate_target(request, done)`, and `deliver(request, done)`. Each returns
an operation handle and completes once with a structured result.
`list_targets` succeeds with `{ targets = { target } }`; an omitted request
target requires exactly one discovered target, while a supplied target must be
among that list. `validate_target` succeeds with the validated target. Deliver
receives copied immutable batch and target values plus submit and
strict-session-guard options; it succeeds only with
`{ outcome = "delivered_to_input" }`. Malformed extension results are rejected
at the boundary. The package acknowledges only the captured ID/revision
members after that outcome. Cancellation before deliver returns `cancelled`.
Once deliver starts, a cancelled, failed, or malformed completion is
`uncertain` and retains drafts; only the confirmed outcome acknowledges them.
Discovery happens once per operation; final validation does not rediscover all
agents. The synthetic capture example records payload bytes without invoking a
model.

## Controller behavior

`session-v1.json` illustrates observed and desired state, an invocation ID,
and owned topology effects. A toggle is resolved once under the session lock
into desired `show` or `hide`. A retry of that invocation reconciles the same
intent. A new toggle creates a new invocation. Explicit show and hide are
idempotent. Shutdown is separate and respects dirty buffers.

Guard the SessionKey and all affected tabs in deterministic key order.
A timed-out mutation keeps unresolved ownership until a late server effect
is fenced or safely reconciled. Never restore a saved layout by recreating
unowned processes. Preserve ambiguous editors and report `ownership_conflict`.
Geometry success requires agreement among the current content rectangle,
PTY dimensions, and Neovim grid before the deadline.

## Fixture checks

Run `python3 tests/contracts/check.py` from the repository root. The fixture
checks validate key distinctions, ranges, revisions, batch membership, and
receipt identity. Later PRs must add behavioral conformance against their
actual implementations; fixture checks alone are not runtime evidence.
