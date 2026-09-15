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

-- F6: the documented gutter option remains valid through both boundaries.
assert(require("herdr_feedback.legacy").validate_setup({ comment_range_style = "gutter" }), "legacy accepts gutter")
vim.g.mapleader = " "
vim.keymap.set("n", "<leader>rc", "<cmd>echo 'custom'<CR>")
local setup = feedback.setup({ comment_range_style = "gutter", keymaps = true })
assert(setup.ok, "public setup accepts gutter")
assert(vim.fn.maparg("<leader>rc", "n"):find("custom", 1, true), "gutter setup preserves user mappings")
assert(feedback.setup({ comment_range_style = "gutter" }).value.already_configured, "gutter setup is idempotent")
assert(not feedback.setup({ comment_range_style = "not-a-style" }).ok, "invalid styles remain rejected")

local listed = call(function(done) return feedback.list({ review = review(root) }, done) end, "list before transport")
assert(listed.ok and #listed.value == 3, "fixture drafts persist")

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
  elseif argv[2] == "agent" and argv[3] == "focus" then error("controlled focus startup failure")
  else error("unexpected child") end
  return {}
end
local sent, sent_callbacks
sent_callbacks = 0
feedback.send({ review = review(root), target = target }, function(value) sent_callbacks = sent_callbacks + 1; sent = value end)
-- Change every relevant ambient selector before scheduled startup.
vim.env.HERDR_SOCKET_PATH = "socket-B"; vim.env.HERDR_BIN_PATH = "herdr-B"; vim.env.HERDR_SESSION = "other"; vim.env.HERDR_CONFIG_PATH = "other-config"
wait_for(function() return pending.preflight ~= nil end, "explicit preflight starts")
local first_command = command_calls[1]
assert(first_command.argv[1] == "herdr-A" and first_command.options.clear_env, "preflight captures executable and isolates environment")
assert(first_command.options.env.HERDR_SOCKET_PATH == "socket-A" and first_command.options.env.HERDR_RUNTIME_LEASE_TOKEN == "fixture-lease", "preflight keeps captured socket and lease")
assert(first_command.options.env.HERDR_SESSION == nil and first_command.options.env.HERDR_CONFIG_PATH == nil, "preflight removes competing selectors")
pending.preflight({ code = 0, stdout = vim.json.encode({ result = { agents = {
  { workspace_id = "workspace", tab_id = "tab", pane_id = "pane", agent_session = { kind = "id", value = "agent" } },
} } }), stderr = "" })
wait_for(function() return pending.delivery ~= nil end, "explicit delivery starts")
assert(command_calls[2].argv[1] == "herdr-A" and command_calls[2].options.env.HERDR_SOCKET_PATH == "socket-A", "delivery uses captured route")
vim.env.HERDR_SOCKET_PATH = "socket-C"
pending.delivery({ code = 0, stdout = "", stderr = "" })
wait_for(function() return sent ~= nil end, "focus failure still completes delivered operation")
assert(sent_callbacks == 1 and sent.ok and sent.value.outcome == "delivered_to_input", "focus failure retains confirmed delivery")
assert(command_calls[3].argv[1] == "herdr-A" and command_calls[3].options.env.HERDR_SOCKET_PATH == "socket-A", "focus uses captured route")
vim.system = old_system

print("PR02 correction regressions: lifecycle, routing, snapshot, and gutter: ok")
vim.cmd("qa!")
