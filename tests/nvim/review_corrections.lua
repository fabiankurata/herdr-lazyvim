-- Regression coverage for PR #2 lifecycle corrections. The fixture uses the
-- real package and synthetic child-process callbacks only.
local repo = assert(vim.env.HERDR_TEST_REPO)
local state_root = assert(vim.env.HERDR_TEST_STATE_ROOT)
vim.opt.runtimepath:prepend(repo .. "/nvim")

local function wait_for(predicate, message)
  assert(vim.wait(1000, predicate, 5), message)
end

local function review(root)
  return {
    worktree = { authority = { host = vim.uv.os_gethostname() or "localhost", path_authority = "local" }, canonical_root = root },
    review_id = "draft",
  }
end

local function state_path(root)
  return vim.fn.stdpath("state") .. "/herdr-review/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
end

local function bytes(root)
  local path = state_path(root)
  return vim.fn.filereadable(path) == 1 and table.concat(vim.fn.readfile(path), "\n") or nil
end

local root = state_root .. "/review-corrections"
vim.fn.mkdir(root .. "/.git", "p")
vim.fn.writefile({ "first", "second" }, root .. "/example.lua")
root = assert(vim.uv.fs_realpath(root))
vim.cmd("edit " .. vim.fn.fnameescape(root .. "/example.lua"))
local feedback = require("herdr_feedback")

local function call(invoke, message)
  local result, callbacks
  callbacks = 0
  local handle = invoke(function(value) callbacks = callbacks + 1; result = value end)
  assert(type(handle) == "table" and type(handle.cancel) == "function", message .. " returns a handle")
  wait_for(function() return result ~= nil end, message .. " completes")
  vim.wait(20)
  assert(callbacks == 1, message .. " completes exactly once")
  return result, handle
end

-- F5: text belongs to the invocation, not the mutable caller table.
for _, replacement in ipairs({ "replacement", vim.NIL, 42 }) do
  local request = { bufnr = vim.api.nvim_get_current_buf(), range = { start_line = 1, end_line = 1 }, text = "captured comment" }
  local result
  local callbacks = 0
  feedback.add(request, function(value) callbacks = callbacks + 1; result = value end)
  request.text = replacement == vim.NIL and nil or replacement
  wait_for(function() return result ~= nil end, "mutable add completes")
  assert(callbacks == 1 and result.ok and result.value.text == "captured comment", "add snapshots validated text")
end

-- A cancellation after the durable commit completed cannot relabel it.
do
  local result, callbacks
  callbacks = 0
  local operation = feedback.add({ bufnr = vim.api.nvim_get_current_buf(), range = { start_line = 1, end_line = 1 }, text = "committed before cancel" }, function(value)
    callbacks = callbacks + 1
    result = value
  end)
  wait_for(function() return result ~= nil end, "committed add completes")
  local committed_bytes = bytes(root)
  operation.cancel()
  vim.wait(30)
  assert(callbacks == 1 and result.ok and bytes(root) == committed_bytes, "late cancellation does not relabel a commit")
end

-- F6: the documented gutter option remains valid through both boundaries.
assert(require("herdr_feedback.legacy").validate_setup({ comment_range_style = "gutter" }), "legacy accepts gutter")
vim.g.mapleader = " "
vim.keymap.set("n", "<leader>rc", "<cmd>echo 'custom'<CR>")
local setup = feedback.setup({ comment_range_style = "gutter", keymaps = true })
assert(setup.ok, "public setup accepts gutter")
assert(vim.fn.maparg("<leader>rc", "n"):find("custom", 1, true), "gutter setup preserves user mappings")
assert(feedback.setup({ comment_range_style = "gutter" }).value.already_configured, "gutter setup is idempotent")
assert(not feedback.setup({ comment_range_style = "not-a-style" }).ok, "invalid styles remain rejected")
vim.cmd("doautocmd BufEnter")
local marks = vim.api.nvim_buf_get_extmarks(0, vim.api.nvim_get_namespaces().herdr_review, 0, -1, { details = true })
assert(#marks > 0 and marks[1][4].sign_text and not marks[1][4].hl_group and not marks[1][4].line_hl_group,
  "gutter rendering has a sign without a range tint")

local listed = call(function(done) return feedback.list({ review = review(root) }, done) end, "list before transport")
assert(listed.ok and #listed.value == 4, "fixture drafts persist")

-- F1: a retained list callback cannot revive an operation whose method returned
-- an invalid handle. The recorder proves validation, delivery, and ack never run.
local retained, stages = nil, {}
local transport = {
  list_targets = function(_, done) stages[#stages + 1] = "list"; retained = done; return {} end,
  validate_target = function(_, _) stages[#stages + 1] = "validate"; return { cancel = function() end } end,
  deliver = function(_, _) stages[#stages + 1] = "deliver"; return { cancel = function() end } end,
}
assert(feedback.register_transport("late-invalid-handle", transport).ok)
local before = bytes(root)
local terminal = call(function(done) return feedback.send({ review = review(root), transport = "late-invalid-handle" }, done) end, "invalid transport handle")
assert(not terminal.ok and terminal.error.code == "invalid_state", "invalid list handle is terminal")
retained({ ok = true, value = { targets = { { name = "late" } } } })
vim.wait(50)
assert(vim.deep_equal(stages, { "list" }), "late list callback has no later side effects")
assert(bytes(root) == before, "late list callback does not acknowledge drafts")

-- Other callback/return orderings share the same terminal-stage invariant.
for name, list_targets in pairs({
  ["callback-then-throw"] = function(done)
    done({ ok = true, value = { targets = { { name = "late" } } } })
    error("controlled list throw")
  end,
  ["sync-callback-invalid-handle"] = function(done)
    done({ ok = true, value = { targets = { { name = "late" } } } })
    return {}
  end,
}) do
  local order = {}
  assert(feedback.register_transport(name, {
    list_targets = function(_, done) order[#order + 1] = "list"; return list_targets(done) end,
    validate_target = function() order[#order + 1] = "validate"; return { cancel = function() end } end,
    deliver = function() order[#order + 1] = "deliver"; return { cancel = function() end } end,
  }).ok)
  local value = call(function(done) return feedback.send({ review = review(root), transport = name }, done) end, name)
  assert(not value.ok and value.error.code == "invalid_state", name .. " is terminal")
  vim.wait(30)
  assert(vim.deep_equal(order, { "list" }), name .. " cannot start later stages")
end

-- Duplicate and late callbacks after cancellation must remain inert.
local cancel_callback, cancel_stages = nil, {}
assert(feedback.register_transport("late-cancel", {
  list_targets = function(_, done) cancel_stages[#cancel_stages + 1] = "list"; cancel_callback = done; return { cancel = function() end } end,
  validate_target = function() cancel_stages[#cancel_stages + 1] = "validate"; return { cancel = function() end } end,
  deliver = function() cancel_stages[#cancel_stages + 1] = "deliver"; return { cancel = function() end } end,
}).ok)
local cancelled, cancelled_calls = nil, 0
local cancel_handle = feedback.send({ review = review(root), transport = "late-cancel" }, function(value) cancelled_calls = cancelled_calls + 1; cancelled = value end)
wait_for(function() return cancel_callback ~= nil end, "cancellable list starts")
cancel_handle.cancel()
cancel_callback({ ok = true, value = { targets = { { name = "late" } } } })
cancel_callback({ ok = true, value = { targets = { { name = "late" } } } })
wait_for(function() return cancelled ~= nil end, "cancelled list completes")
assert(cancelled_calls == 1 and not cancelled.ok and cancelled.error.code == "cancelled", "cancellation completes once")
assert(vim.deep_equal(cancel_stages, { "list" }), "late cancelled callbacks have no side effects")

-- Cancellation may run from an extension before that extension has returned a
-- handle. A later valid return must not revive the terminal stage.
local operations = require("herdr_feedback.operations")
local scheduled, in_method_operation = operations.scheduled, nil
operations.scheduled = function(done, work)
  local operation, state, finish = scheduled(done, work)
  in_method_operation = operation
  return operation, state, finish
end
local returning_stages = {}
assert(feedback.register_transport("cancel-before-handle", {
  list_targets = function(_, _)
    returning_stages[#returning_stages + 1] = "list"
    in_method_operation.cancel()
    return { cancel = function() end }
  end,
  validate_target = function() returning_stages[#returning_stages + 1] = "validate"; return { cancel = function() end } end,
  deliver = function() returning_stages[#returning_stages + 1] = "deliver"; return { cancel = function() end } end,
}).ok)
local returning = call(function(done) return feedback.send({ review = review(root), transport = "cancel-before-handle" }, done) end, "cancel before returned handle")
operations.scheduled = scheduled
assert(not returning.ok and returning.error.code == "cancelled" and vim.deep_equal(returning_stages, { "list" }),
  "cancellation during extension return leaves the stage terminal: " .. vim.inspect(returning) .. " " .. vim.inspect(returning_stages))

-- A confirmed registered delivery remains delivered when acknowledgement code
-- throws after the delivery boundary.
local original_acknowledge = operations.acknowledge
operations.acknowledge = function() error("controlled acknowledgement throw") end
local acknowledged_transport = {
  list_targets = function(_, done) done({ ok = true, value = { targets = { { name = "target" } } } }); return { cancel = function() end } end,
  validate_target = function(_, done) done({ ok = true, value = { name = "target" } }); return { cancel = function() end } end,
  deliver = function(_, done) done({ ok = true, value = { outcome = "delivered_to_input" } }); return { cancel = function() end } end,
}
assert(feedback.register_transport("acknowledgement-throw", acknowledged_transport).ok)
local acknowledged = call(function(done) return feedback.send({ review = review(root), transport = "acknowledgement-throw" }, done) end, "acknowledgement throw")
operations.acknowledge = original_acknowledge
assert(acknowledged.ok and acknowledged.value.outcome == "delivered_to_input" and acknowledged.value.warning,
  "acknowledgement throw retains confirmed delivery")

-- F4: a package exception produces one structured completion rather than
-- escaping the scheduler and abandoning the caller.
local completion = require("herdr_feedback.completion")
local thrown = call(function(done)
  return completion.scheduled(done, function() error("controlled scheduled failure") end)
end, "scheduled exception")
assert(not thrown.ok and thrown.error.code == "failed", "scheduled exception is structured")

-- F4: a throw after a kernel lock is held releases it. The next transaction
-- must acquire the same lock and write successfully.
local store = require("herdr_feedback.store")
local original_random = vim.uv.random
local first = store.update(review(root), function(records)
  vim.uv.random = function() error("controlled temporary identity failure") end
  return records, true
end)
vim.uv.random = original_random
assert(first == nil, "thrown transaction reports failure")
local second, second_error = store.update(review(root), function(records) return records, true end)
assert(second and not second_error, "writer after thrown transaction acquires released lock")

-- Retain a primary transaction exception when lock cleanup also reports a
-- failure, while still proving the next writer owns a released descriptor.
local original_open, original_close = vim.uv.fs_open, vim.uv.fs_close
local lock_descriptor
vim.uv.fs_open = function(path, flags, mode, callback)
  local file, message, code = original_open(path, flags, mode, callback)
  if not callback and file and path:find(".lock-v2", 1, true) then lock_descriptor = file end
  return file, message, code
end
vim.uv.fs_close = function(file, callback)
  if not callback and file == lock_descriptor then original_close(file); return nil, "controlled cleanup failure" end
  return original_close(file, callback)
end
local cleanup_random = vim.uv.random
local primary, primary_error = store.update(review(root), function(records)
  vim.uv.random = function() error("controlled primary failure") end
  return records, true
end)
vim.uv.random, vim.uv.fs_open, vim.uv.fs_close = cleanup_random, original_open, original_close
assert(primary == nil and primary_error:find("controlled primary failure", 1, true)
    and primary_error:find("additionally could not release", 1, true), "primary plus cleanup failure retains both contexts")
local recovered, recovered_error = store.update(review(root), function(records) return records, true end)
assert(recovered and not recovered_error, "writer after cleanup diagnostic acquires released lock")

-- A missing bundled executable is a definite startup failure, not a lost done.
vim.env.HERDR_SOCKET_PATH = "missing-socket"
local missing_target = {
  connection = { authority = review(root).worktree.authority, socket = "missing-socket" },
  workspace_id = "workspace", tab_id = "tab", pane_id = "pane", agent_session_id = "agent", worktree = review(root).worktree,
}
local original_system = vim.system
vim.system = function() error("controlled executable startup failure") end
local missing = call(function(done) return feedback.send({ review = review(root), target = missing_target }, done) end, "missing executable")
assert(not missing.ok and missing.error.code == "failed", "unstartable bundled executable is structured")
vim.system = original_system

-- F2 and F4: explicit target subprocesses retain the invocation executable and
-- socket, and a post-delivery focus throw cannot lose confirmed completion.
vim.env.HERDR_SOCKET_PATH = "socket-A"
vim.env.HERDR_BIN_PATH = "herdr-A"
vim.env.HERDR_RUNTIME_LEASE_TOKEN = "fixture-lease"
local target = {
  connection = { authority = review(root).worktree.authority, socket = "socket-A" },
  workspace_id = "workspace", tab_id = "tab", pane_id = "pane", agent_session_id = "agent", worktree = review(root).worktree,
}
local command_calls, pending = {}, {}
local old_system = vim.system
vim.system = function(argv, options, done)
  command_calls[#command_calls + 1] = { argv = vim.deepcopy(argv), options = vim.deepcopy(options) }
  if argv[2] == "agent" and argv[3] == "list" then pending.preflight = done
  elseif argv[2] == "pane" then pending.delivery = done
  elseif argv[2] == "agent" and argv[3] == "focus" then pending.focus = done or true
  else error("unexpected child") end
  return {}
end
local sent, sent_callbacks
sent_callbacks = 0
feedback.send({ review = review(root), target = target }, function(value) sent_callbacks = sent_callbacks + 1; sent = value end)
-- Change every relevant ambient selector before scheduled startup.
vim.env.HERDR_SOCKET_PATH = "socket-B"; vim.env.HERDR_BIN_PATH = "herdr-B"; vim.env.HERDR_SESSION = "other"; vim.env.HERDR_CONFIG_PATH = "other-config"; vim.env.HERDR_SERVER_SESSION = "other-server"
wait_for(function() return pending.preflight ~= nil end, "explicit preflight starts")
local first_command = command_calls[1]
assert(first_command.argv[1] == "herdr-A" and first_command.options.clear_env, "preflight captures executable and isolates environment")
assert(first_command.options.env.HERDR_SOCKET_PATH == "socket-A" and first_command.options.env.HERDR_RUNTIME_LEASE_TOKEN == "fixture-lease", "preflight keeps captured socket and lease")
assert(first_command.options.env.HERDR_SESSION == nil and first_command.options.env.HERDR_CONFIG_PATH == nil
    and first_command.options.env.HERDR_SERVER_SESSION == nil, "preflight removes competing selectors")
pending.preflight({ code = 0, stdout = vim.json.encode({ result = { agents = {
  { workspace_id = "workspace", tab_id = "tab", pane_id = "pane", agent_session = { kind = "id", value = "agent" } },
} } }), stderr = "" })
vim.env.HERDR_SOCKET_PATH = "socket-C"; vim.env.HERDR_BIN_PATH = "herdr-C"; vim.env.HERDR_CLIENT_SOCKET_PATH = "competing"
wait_for(function() return pending.delivery ~= nil end, "explicit delivery starts")
assert(command_calls[2].argv[1] == "herdr-A" and command_calls[2].options.env.HERDR_SOCKET_PATH == "socket-A", "delivery uses captured route")
assert(command_calls[2].options.env.HERDR_CLIENT_SOCKET_PATH == nil, "delivery removes competing routing selector")
vim.env.HERDR_SOCKET_PATH = "socket-D"; vim.env.HERDR_BIN_PATH = "herdr-D"; vim.env.HERDR_SESSION = "third"
pending.delivery({ code = 0, stdout = "", stderr = "" })
wait_for(function() return pending.focus ~= nil end, "focus starts after delivery")
if type(pending.focus) == "function" then pending.focus({ code = 1, stdout = "", stderr = "controlled focus failure" }) end
wait_for(function() return sent ~= nil end, "focus failure still completes delivered operation")
assert(sent_callbacks == 1 and sent.ok and sent.value.outcome == "delivered_to_input" and sent.value.warning, "focus failure retains confirmed delivery")
assert(command_calls[3].argv[1] == "herdr-A" and command_calls[3].options.env.HERDR_SOCKET_PATH == "socket-A", "focus uses captured route")
assert(command_calls[3].options.env.HERDR_SESSION == nil and command_calls[3].options.env.HERDR_SERVER_SESSION == nil, "focus removes competing selectors")
vim.system = old_system

print("PR02 correction regressions: lifecycle, routing, snapshot, and gutter: ok")
vim.cmd("qa!")
