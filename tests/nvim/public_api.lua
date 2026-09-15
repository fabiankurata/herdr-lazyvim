local repo = assert(vim.env.HERDR_TEST_REPO, "HERDR_TEST_REPO is required")
local state_root = assert(vim.env.HERDR_TEST_STATE_ROOT, "HERDR_TEST_STATE_ROOT is required")

assert(vim.fn.stdpath("state"):sub(1, #state_root) == state_root, "public API test must use isolated state")
local function file_bytes(path)
  local file = assert(io.open(path, "rb"), "missing " .. path)
  local value = assert(file:read("*a"))
  assert(file:close())
  return value
end
for _, name in ipairs({ "completion", "context", "init", "legacy", "operations", "store" }) do
  local copied = file_bytes(repo .. "/nvim/lua/herdr_feedback/" .. name .. ".lua")
  local root = file_bytes(repo .. "/lua/herdr_feedback/" .. name .. ".lua")
  assert(not copied:find("dofile", 1, true), "copied nvim runtime owns " .. name)
  assert(root:find("/nvim/lua/herdr_feedback/" .. name .. ".lua", 1, true), "root runtime forwards " .. name)
end
assert(file_bytes(repo .. "/doc/herdr-feedback.txt") == file_bytes(repo .. "/nvim/doc/herdr-feedback.txt"), "both help layouts stay identical")
vim.opt.runtimepath:prepend(repo)
vim.cmd("runtime plugin/herdr-feedback.lua")

local function equals(actual, expected, message)
  assert(actual == expected, string.format("%s: expected %s, got %s", message, vim.inspect(expected), vim.inspect(actual)))
end

local function wait_for(predicate, message)
  assert(vim.wait(500, predicate, 5), message)
end

local function call(invoke, message)
  local result, callbacks = nil, 0
  local operation = invoke(function(value)
    callbacks = callbacks + 1
    result = value
  end)
  assert(type(operation) == "table" and type(operation.cancel) == "function", message .. " returns a cancellation handle")
  assert(result == nil, message .. " does not complete inline")
  wait_for(function() return result ~= nil end, message .. " completes")
  vim.wait(30)
  equals(callbacks, 1, message .. " calls done once")
  return result
end

local function review(root)
  return {
    worktree = {
      authority = { host = vim.uv.os_gethostname() or "localhost", path_authority = "local" },
      canonical_root = root,
    },
    review_id = "draft",
  }
end

local function state_file(root)
  return vim.fn.stdpath("state") .. "/herdr-review/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
end

local function read_state(root)
  local path = state_file(root)
  if vim.fn.filereadable(path) == 0 then return nil end
  return vim.json.decode(table.concat(vim.fn.readfile(path), "\n"))
end

local function state_bytes(root)
  local path = state_file(root)
  if vim.fn.filereadable(path) == 0 then return nil end
  return table.concat(vim.fn.readfile(path), "\n")
end

local root_a = state_root .. "/api-a"
local root_b = state_root .. "/api-b"
for _, root in ipairs({ root_a, root_b }) do
  vim.fn.mkdir(root .. "/.git", "p")
end
vim.fn.writefile({ "A first", "A second" }, root_a .. "/example.lua")
vim.fn.writefile({ "B first", "B second" }, root_b .. "/example.lua")
root_a = assert(vim.uv.fs_realpath(root_a))
root_b = assert(vim.uv.fs_realpath(root_b))

vim.env.HERDR_WORKSPACE_ID = nil
vim.env.HERDR_PANE_ID = nil
vim.env.HERDR_BIN_PATH = nil
vim.g.mapleader = " "
vim.keymap.set("n", "<leader>rc", "<cmd>echo 'custom review map'<CR>")

local feedback = require("herdr_feedback")
for _, invalid_options in ipairs({
  "not a table",
  { keymaps = "yes" },
  { comment_display = "virtual_text" },
  { comment_card_border = "orange" },
  { comment_save_keys = { "<C-s>", false } },
}) do
  local invalid_setup = feedback.setup(invalid_options)
  assert(not invalid_setup.ok and invalid_setup.error.code == "invalid_request", "invalid setup is structured before configuration")
end
local first_setup = feedback.setup({ keymaps = true })
assert(first_setup.ok and first_setup.value.configured, "setup succeeds without Herdr")
local repeated_setup = feedback.setup({ keymaps = true })
assert(repeated_setup.ok and repeated_setup.value.already_configured, "setup is idempotent")
assert(vim.fn.exists(":HerdrReviewComment") == 2, "root runtimepath exposes compatibility commands")
assert(vim.fn.maparg("<leader>rc", "n"):find("custom review map", 1, true), "setup keeps a custom mapping")

local adapter = {
  resolve = function() end,
  capture = function() end,
  navigate = function() end,
}
assert(feedback.register_source_adapter("fixture", adapter).ok, "source adapter registration succeeds")
assert(not feedback.register_transport("bad", {}).ok, "transport registration validates its boundary")

vim.cmd("edit " .. vim.fn.fnameescape(root_a .. "/example.lua"))
local buffer_a = vim.api.nvim_get_current_buf()
local captured_add, captured_callbacks = nil, 0
local captured_handle = feedback.add({
  bufnr = buffer_a,
  range = { start_line = 1, end_line = 1 },
  text = "captured before the root switch",
}, function(value)
  captured_callbacks = captured_callbacks + 1
  captured_add = value
end)
assert(type(captured_handle.cancel) == "function", "add returns a cancellation handle")
vim.cmd("edit " .. vim.fn.fnameescape(root_b .. "/example.lua"))
wait_for(function() return captured_add ~= nil end, "add completes after the root switch")
equals(captured_callbacks, 1, "add callback count")
assert(captured_add.ok and captured_add.value.file == "example.lua", "documented add captures complete lines")

local listed_a = call(function(done) return feedback.list({ review = review(root_a) }, done) end, "list A")
assert(listed_a.ok and #listed_a.value == 1 and listed_a.value[1].text == "captured before the root switch", "add persists in the invocation root")
local listed_b = call(function(done) return feedback.list({ review = review(root_b) }, done) end, "list B")
assert(listed_b.ok and #listed_b.value == 0, "root B stays separate")
local supported_bytes = state_bytes(root_a)
local remote_review = review(root_a)
remote_review.worktree.authority.host = "remote-fixture"
local remote_list = call(function(done) return feedback.list({ review = remote_review }, done) end, "remote review list")
assert(not remote_list.ok and remote_list.error.code == "invalid_request", "unsupported authority cannot read local draft")
local other_review = review(root_a)
other_review.review_id = "other"
local other_edit = call(function(done)
  return feedback.edit(captured_add.value.id, { review = other_review, expected_revision = 1, text = "must not write" }, done)
end, "other review edit")
assert(not other_edit.ok and other_edit.error.code == "invalid_request", "unsupported review cannot write local draft")
equals(state_bytes(root_a), supported_bytes, "unsupported complete keys preserve local draft bytes")

local legacy_items
vim.ui.select = function(items, options, done)
  if options.prompt == "Review comments" then legacy_items = vim.deepcopy(items) end
  done(nil)
end
vim.cmd("edit " .. vim.fn.fnameescape(root_a .. "/example.lua"))
require("herdr_review").list()
assert(#legacy_items == 1 and legacy_items[1].text == "captured before the root switch", "legacy UI reads public API records")

local edited = call(function(done)
  return feedback.edit(captured_add.value.id, {
    review = review(root_a), expected_revision = 1, text = "edited through the public API",
  }, done)
end, "edit")
assert(edited.ok and edited.value.revision == 2, "edit advances the revision")
local conflict = call(function(done)
  return feedback.edit(captured_add.value.id, {
    review = review(root_a), expected_revision = 1, text = "stale write",
  }, done)
end, "stale edit")
assert(not conflict.ok and conflict.error.code == "revision_conflict" and conflict.error.local_text == "edited through the public API", "stale edit reports the retained text")
local exported = call(function(done)
  return feedback.export({ review = review(root_a), annotation_ids = { captured_add.value.id } }, done)
end, "export")
assert(exported.ok and exported.value.payload == "example.lua:1\nA first\nedited through the public API", "export reads the shared record")
local deleted_draft = call(function(done)
  return feedback.add({ bufnr = buffer_a, range = { start_line = 2, end_line = 2 }, text = "delete through the API" }, done)
end, "add deletable draft")
assert(deleted_draft.ok, "deletable draft persists")
local deleted_draft_result = call(function(done)
  return feedback.delete(deleted_draft.value.id, { review = review(root_a), expected_revision = 1 }, done)
end, "delete")
assert(deleted_draft_result.ok, "delete removes the requested revision")

local cancelled, cancel_callbacks = nil, 0
local cancelled_handle = feedback.add({
  bufnr = buffer_a,
  range = { start_line = 2, end_line = 2 },
  text = "must not persist",
}, function(value)
  cancel_callbacks = cancel_callbacks + 1
  cancelled = value
end)
cancelled_handle.cancel()
wait_for(function() return cancelled ~= nil end, "cancelled add completes")
equals(cancel_callbacks, 1, "cancelled add callback count")
assert(not cancelled.ok and cancelled.error.code == "cancelled", "pre-mutation cancellation is explicit")
equals(#(call(function(done) return feedback.list({ review = review(root_a) }, done) end, "list after cancelled add").value), 1, "cancelled add leaves the envelope unchanged")

local calls, pending = {}, {}
vim.env.HERDR_WORKSPACE_ID = "fixture-workspace"
vim.env.HERDR_PANE_ID = "fixture-editor"
vim.env.HERDR_BIN_PATH = "FAKE-HERDR-PUBLIC"
local function target_for(root, pane_id)
  local key = review(root)
  return {
    connection = {
      authority = vim.deepcopy(key.worktree.authority),
      socket = assert(vim.env.HERDR_SOCKET_PATH, "isolated target requires a socket"),
    },
    workspace_id = "fixture-workspace",
    tab_id = "fixture-tab",
    pane_id = pane_id,
    agent_session_id = "fixture-agent-session",
    worktree = vim.deepcopy(key.worktree),
  }
end

local function occupant_for(target, changed)
  local occupant = {
    workspace_id = target.workspace_id,
    tab_id = target.tab_id,
    pane_id = target.pane_id,
    agent_session = { kind = "id", value = target.agent_session_id },
    agent = "fixture-agent",
    agent_status = "idle",
  }
  for key, value in pairs(changed or {}) do
    if key == "agent_session_id" then
      occupant.agent_session.value = value
    else
      occupant[key] = value
    end
  end
  return occupant
end

local function preflight(target, changed)
  assert(type(pending.preflight) == "function", "target send starts target preflight")
  pending.preflight({
    code = 0,
    stdout = vim.json.encode({ result = { agents = { occupant_for(target, changed) } } }),
    stderr = "",
  })
end

vim.system = function(argv, _, done)
  table.insert(calls, vim.deepcopy(argv))
  if argv[2] == "agent" and argv[3] == "list" then
    pending.preflight = done
  elseif argv[2] == "pane" and argv[3] == "send-text" then
    pending.delivery = done
    pending.payload = argv[5]
  elseif argv[2] == "agent" and argv[3] == "focus" then
    pending.focus = true
    vim.schedule(function() done({ code = 0, stdout = "", stderr = "" }) end)
  else
    error("unexpected fake command: " .. vim.inspect(argv))
  end
  return {}
end

local mismatched_target = call(function(done)
  return feedback.send({ review = review(root_a), target = target_for(root_b, "wrong-root-agent") }, done)
end, "mismatched target")
assert(not mismatched_target.ok and mismatched_target.error.code == "invalid_request", "target must belong to the review worktree")
equals(#calls, 0, "mismatched target sends no bytes")

local send_result, send_callbacks = nil, 0
local explicit_target = target_for(root_a, "explicit-agent")
local send_handle = feedback.send({
  review = review(root_a), annotation_ids = { captured_add.value.id }, target = explicit_target, submit = false,
}, function(value)
  send_callbacks = send_callbacks + 1
  send_result = value
end)
assert(type(send_handle.cancel) == "function", "send returns a cancellation handle")
wait_for(function() return pending.preflight ~= nil end, "target send starts target preflight")
equals(#calls, 1, "target preflight has not sent input")
preflight(explicit_target)
wait_for(function() return pending.delivery ~= nil end, "matching target preflight starts fake delivery")
assert(calls[2][4] == "explicit-agent", "target send uses the supplied pane")
assert(send_result == nil, "send does not report delivery before fake vim.system completion")

vim.cmd("edit " .. vim.fn.fnameescape(root_b .. "/example.lua"))
local added_b = call(function(done)
  return feedback.add({ bufnr = vim.api.nvim_get_current_buf(), range = { start_line = 1, end_line = 1 }, text = "B survives A acknowledgement" }, done)
end, "add B during A send")
assert(added_b.ok, "B add succeeds while A send is pending")
pending.delivery({ code = 0, stdout = "", stderr = "" })
pending.delivery({ code = 0, stdout = "", stderr = "" })
wait_for(function() return send_result ~= nil end, "send completes after delivery")
vim.wait(30)
equals(send_callbacks, 1, "send callback count")
equals(#calls, 3, "target send preflights, sends, and acknowledges once")
assert(send_result.ok and send_result.value.outcome == "delivered_to_input" and type(send_result.value.batch_id) == "string", "send returns its confirmed outcome")
equals(pending.payload, "\27[200~example.lua:1\nA first\nedited through the public API\27[201~", "send passes exact fake bytes")
assert(read_state(root_a) == nil, "A acknowledgement clears only the captured revision")
assert(read_state(root_b).comments[1].text == "B survives A acknowledgement", "A acknowledgement leaves B untouched")

local cancellable_draft = call(function(done)
  return feedback.add({ bufnr = vim.api.nvim_get_current_buf(), range = { start_line = 2, end_line = 2 }, text = "cancel this delivery" }, done)
end, "add cancellable send draft")
assert(cancellable_draft.ok, "cancellable send draft persists")
calls, pending = {}, {}
local send_cancelled, send_cancelled_callbacks = nil, 0
local cancelled_send_handle = feedback.send({ review = review(root_b), target = target_for(root_b, "cancelled-agent") }, function(value)
  send_cancelled_callbacks = send_cancelled_callbacks + 1
  send_cancelled = value
end)
cancelled_send_handle.cancel()
wait_for(function() return send_cancelled ~= nil end, "cancelled send completes")
equals(send_cancelled_callbacks, 1, "cancelled send callback count")
assert(not send_cancelled.ok and send_cancelled.error.code == "cancelled", "pre-discovery cancellation is explicit")
equals(#calls, 0, "cancelled send has no delivery side effect")
equals(#read_state(root_b).comments, 2, "cancelled send keeps drafts")

for _, mismatch in ipairs({
  { field = "workspace_id", value = "other-workspace" },
  { field = "tab_id", value = "other-tab" },
  { field = "pane_id", value = "other-pane" },
  { field = "agent_session_id", value = "other-agent-session" },
}) do
  calls, pending = {}, {}
  local target = target_for(root_b, "mismatch-agent")
  local mismatch_result, mismatch_callbacks = nil, 0
  feedback.send({ review = review(root_b), target = target }, function(value)
    mismatch_callbacks = mismatch_callbacks + 1
    mismatch_result = value
  end)
  wait_for(function() return pending.preflight ~= nil end, mismatch.field .. " mismatch starts preflight")
  preflight(target, { [mismatch.field] = mismatch.value })
  wait_for(function() return mismatch_result ~= nil end, mismatch.field .. " mismatch completes")
  vim.wait(30)
  equals(mismatch_callbacks, 1, mismatch.field .. " mismatch callback count")
  assert(not mismatch_result.ok and mismatch_result.error.code == "target_mismatch", mismatch.field .. " mismatch is structured")
  equals(#calls, 1, mismatch.field .. " mismatch sends no input bytes")
  equals(#read_state(root_b).comments, 2, mismatch.field .. " mismatch keeps drafts")
end

calls, pending = {}, {}
local missing_target, missing_target_callbacks = nil, 0
feedback.send({ review = review(root_b), target = target_for(root_b, "missing-agent") }, function(value)
  missing_target_callbacks = missing_target_callbacks + 1
  missing_target = value
end)
wait_for(function() return pending.preflight ~= nil end, "missing target starts preflight")
pending.preflight({ code = 0, stdout = vim.json.encode({ result = { agents = {} } }), stderr = "" })
wait_for(function() return missing_target ~= nil end, "missing target completes")
vim.wait(30)
equals(missing_target_callbacks, 1, "missing target callback count")
assert(not missing_target.ok and missing_target.error.code == "target_mismatch", "missing target is structured")
equals(#calls, 1, "missing target sends no input bytes")

calls, pending = {}, {}
local failed_preflight, failed_preflight_callbacks = nil, 0
feedback.send({ review = review(root_b), target = target_for(root_b, "failed-preflight-agent") }, function(value)
  failed_preflight_callbacks = failed_preflight_callbacks + 1
  failed_preflight = value
end)
wait_for(function() return pending.preflight ~= nil end, "failed preflight starts")
pending.preflight({ code = 1, stdout = "", stderr = "fixture preflight failure" })
wait_for(function() return failed_preflight ~= nil end, "failed preflight completes")
vim.wait(30)
equals(failed_preflight_callbacks, 1, "failed preflight callback count")
assert(not failed_preflight.ok and failed_preflight.error.code == "failed", "preflight command failure is structured")
equals(#calls, 1, "preflight command failure sends no input bytes")

calls, pending = {}, {}
local preflight_cancelled, preflight_cancelled_callbacks = nil, 0
local pending_target = target_for(root_b, "pending-preflight-agent")
local pending_cancel_handle = feedback.send({ review = review(root_b), target = pending_target }, function(value)
  preflight_cancelled_callbacks = preflight_cancelled_callbacks + 1
  preflight_cancelled = value
end)
wait_for(function() return pending.preflight ~= nil end, "pending preflight starts")
pending_cancel_handle.cancel()
preflight(pending_target)
preflight(pending_target)
wait_for(function() return preflight_cancelled ~= nil end, "pending preflight cancellation completes")
vim.wait(30)
equals(preflight_cancelled_callbacks, 1, "pending preflight cancellation callback count")
assert(not preflight_cancelled.ok and preflight_cancelled.error.code == "cancelled", "cancellation during preflight is explicit")
equals(#calls, 1, "cancelled preflight sends no input bytes")
equals(#read_state(root_b).comments, 2, "cancelled preflight keeps drafts")

calls, pending = {}, {}
local strict_send = call(function(done)
  return feedback.send({ review = review(root_b), target = target_for(root_b, "strict-agent"), strict_session_guard = true }, done)
end, "strict target send")
assert(not strict_send.ok and strict_send.error.code == "unsupported_capability", "strict guard fails before delivery")
equals(#calls, 0, "strict guard sends no bytes")

calls, pending = {}, {}
local failed_send, failed_send_callbacks = nil, 0
local submit_target = target_for(root_b, "submit-agent")
feedback.send({
  review = review(root_b), annotation_ids = { cancellable_draft.value.id }, target = submit_target, submit = true,
}, function(value)
  failed_send_callbacks = failed_send_callbacks + 1
  failed_send = value
end)
wait_for(function() return pending.preflight ~= nil end, "submit send starts preflight")
preflight(submit_target)
wait_for(function() return pending.delivery ~= nil end, "failed send reaches fake delivery")
assert(pending.payload:sub(-1) == "\r", "submit appends the terminal submit byte")
assert(calls[2][4] == "submit-agent", "submit uses the supplied target pane")
pending.delivery({ code = 1, stdout = "", stderr = "fixture failure" })
wait_for(function() return failed_send ~= nil end, "failed send completes")
equals(failed_send_callbacks, 1, "failed send callback count")
assert(not failed_send.ok and failed_send.error.code == "uncertain", "unconfirmed delivery is structured")
equals(#read_state(root_b).comments, 2, "delivery failure preserves drafts")

local capture_target = { name = "synthetic-capture", destination = "fixture" }
local capture_stages, capture_pending, capture_cancellations = {}, {}, {}
local capture_payload
local capture_transport = {
  list_targets = function(request, done)
    table.insert(capture_stages, "list_targets")
    assert(vim.deep_equal(request.review, review(root_b)), "transport list receives the captured review")
    capture_pending.list_targets = done
    return { cancel = function() table.insert(capture_cancellations, "list_targets") end }
  end,
  validate_target = function(request, done)
    table.insert(capture_stages, "validate_target")
    assert(vim.deep_equal(request.target, capture_target), "transport validates the discovered target")
    capture_pending.validate_target = done
    return { cancel = function() table.insert(capture_cancellations, "validate_target") end }
  end,
  deliver = function(request, done)
    table.insert(capture_stages, "deliver")
    capture_payload = request.batch.payload
    assert(request.batch.transport == "capture", "transport receives its selected name")
    assert(request.submit and not request.strict_session_guard, "transport receives delivery options")
    assert(vim.deep_equal(request.target, capture_target), "transport receives the validated target")
    request.batch.payload = "transport must not mutate the captured batch"
    request.batch.members[1].revision = 999
    capture_pending.deliver = done
    return { cancel = function() table.insert(capture_cancellations, "deliver") end }
  end,
}
assert(feedback.register_transport("capture", capture_transport).ok, "capture transport registration succeeds")
local capture_result, capture_callbacks = nil, 0
local capture_handle = feedback.send({
  review = review(root_b), annotation_ids = { cancellable_draft.value.id }, transport = "capture", submit = true,
}, function(value)
  capture_callbacks = capture_callbacks + 1
  capture_result = value
end)
assert(type(capture_handle.cancel) == "function", "transport send returns a cancellation handle")
wait_for(function() return capture_pending.list_targets ~= nil end, "transport list starts")
capture_pending.list_targets({ ok = true, value = { targets = { capture_target } } })
wait_for(function() return capture_pending.validate_target ~= nil end, "transport validation starts")
capture_pending.validate_target({ ok = true, value = capture_target })
wait_for(function() return capture_pending.deliver ~= nil end, "transport delivery starts")
equals(capture_payload, "example.lua:2\nB second\ncancel this delivery", "transport receives literal exported payload bytes")
capture_pending.deliver({ ok = true, value = { outcome = "delivered_to_input" } })
capture_pending.deliver({ ok = true, value = { outcome = "delivered_to_input" } })
wait_for(function() return capture_result ~= nil end, "transport delivery completes")
vim.wait(30)
equals(capture_callbacks, 1, "transport callback count")
assert(capture_result.ok and capture_result.value.outcome == "delivered_to_input" and capture_result.value.transport == "capture", "transport returns confirmed delivery")
assert(vim.deep_equal(capture_stages, { "list_targets", "validate_target", "deliver" }), "transport methods run once in order")
equals(#capture_cancellations, 0, "confirmed transport delivery is not cancelled")
equals(#read_state(root_b).comments, 1, "transport acknowledgement uses the captured revision despite transport mutation")
equals(read_state(root_b).comments[1].text, "B survives A acknowledgement", "transport acknowledgement preserves the unselected draft")

local cancellation_pending, cancellation_stages, cancellation_calls = {}, {}, {}
local cancellation_transport = {
  list_targets = function(_, done)
    table.insert(cancellation_stages, "list_targets")
    cancellation_pending.list_targets = done
    return { cancel = function() table.insert(cancellation_calls, "list_targets") end }
  end,
  validate_target = function(_, done)
    table.insert(cancellation_stages, "validate_target")
    cancellation_pending.validate_target = done
    return { cancel = function() table.insert(cancellation_calls, "validate_target") end }
  end,
  deliver = function(_, _)
    table.insert(cancellation_stages, "deliver")
    return { cancel = function() table.insert(cancellation_calls, "deliver") end }
  end,
}
assert(feedback.register_transport("cancel-capture", cancellation_transport).ok, "cancellation transport registration succeeds")
local cancellation_result, cancellation_callbacks = nil, 0
local cancellation_handle = feedback.send({ review = review(root_b), transport = "cancel-capture" }, function(value)
  cancellation_callbacks = cancellation_callbacks + 1
  cancellation_result = value
end)
wait_for(function() return cancellation_pending.list_targets ~= nil end, "cancellation transport list starts")
cancellation_pending.list_targets({ ok = true, value = { targets = { capture_target } } })
wait_for(function() return cancellation_pending.validate_target ~= nil end, "cancellation transport validation starts")
cancellation_handle.cancel()
cancellation_pending.validate_target({ ok = true, value = capture_target })
cancellation_pending.validate_target({ ok = true, value = capture_target })
wait_for(function() return cancellation_result ~= nil end, "cancellation transport completes")
vim.wait(30)
equals(cancellation_callbacks, 1, "cancellation transport callback count")
assert(not cancellation_result.ok and cancellation_result.error.code == "cancelled", "cancellation before transport delivery is explicit")
assert(vim.deep_equal(cancellation_stages, { "list_targets", "validate_target" }), "cancelled transport never delivers")
assert(vim.deep_equal(cancellation_calls, { "validate_target" }), "cancellation reaches the pending transport operation")
equals(#read_state(root_b).comments, 1, "cancelled transport keeps drafts")

local inflight_pending, inflight_stages, inflight_cancellations = {}, {}, {}
local inflight_transport = {
  list_targets = function(_, done)
    table.insert(inflight_stages, "list_targets")
    inflight_pending.list_targets = done
    return { cancel = function() table.insert(inflight_cancellations, "list_targets") end }
  end,
  validate_target = function(_, done)
    table.insert(inflight_stages, "validate_target")
    inflight_pending.validate_target = done
    return { cancel = function() table.insert(inflight_cancellations, "validate_target") end }
  end,
  deliver = function(_, done)
    table.insert(inflight_stages, "deliver")
    inflight_pending.deliver = done
    return { cancel = function() table.insert(inflight_cancellations, "deliver") end }
  end,
}
assert(feedback.register_transport("inflight-capture", inflight_transport).ok, "inflight transport registration succeeds")
local inflight_result, inflight_callbacks = nil, 0
local inflight_handle = feedback.send({ review = review(root_b), transport = "inflight-capture" }, function(value)
  inflight_callbacks = inflight_callbacks + 1
  inflight_result = value
end)
wait_for(function() return inflight_pending.list_targets ~= nil end, "inflight transport list starts")
inflight_pending.list_targets({ ok = true, value = { targets = { capture_target } } })
wait_for(function() return inflight_pending.validate_target ~= nil end, "inflight transport validation starts")
inflight_pending.validate_target({ ok = true, value = capture_target })
wait_for(function() return inflight_pending.deliver ~= nil end, "inflight transport delivery starts")
inflight_handle.cancel()
inflight_pending.deliver({ ok = false, error = { code = "cancelled", message = "transport cancelled", context = {} } })
inflight_pending.deliver({ ok = false, error = { code = "cancelled", message = "transport cancelled", context = {} } })
wait_for(function() return inflight_result ~= nil end, "inflight transport cancellation completes")
vim.wait(30)
equals(inflight_callbacks, 1, "inflight transport callback count")
assert(not inflight_result.ok and inflight_result.error.code == "uncertain", "inflight cancellation remains uncertain")
assert(vim.deep_equal(inflight_stages, { "list_targets", "validate_target", "deliver" }), "inflight transport reaches delivery once")
assert(vim.deep_equal(inflight_cancellations, { "deliver" }), "inflight cancellation reaches the delivery operation")
equals(#read_state(root_b).comments, 1, "unconfirmed inflight delivery does not acknowledge drafts")

local malformed_pending, malformed_stages = {}, {}
local malformed_transport = {
  list_targets = function(_, done)
    table.insert(malformed_stages, "list_targets")
    malformed_pending.list_targets = done
    return { cancel = function() end }
  end,
  validate_target = function(_, _)
    table.insert(malformed_stages, "validate_target")
    return { cancel = function() end }
  end,
  deliver = function(_, _)
    table.insert(malformed_stages, "deliver")
    return { cancel = function() end }
  end,
}
assert(feedback.register_transport("malformed-capture", malformed_transport).ok, "malformed transport registration succeeds")
local malformed_result, malformed_callbacks = nil, 0
local malformed_handle = feedback.send({ review = review(root_b), transport = "malformed-capture" }, function(value)
  malformed_callbacks = malformed_callbacks + 1
  malformed_result = value
end)
assert(type(malformed_handle.cancel) == "function", "malformed transport send returns a cancellation handle")
wait_for(function() return malformed_pending.list_targets ~= nil end, "malformed transport list starts")
malformed_pending.list_targets({ ok = true, value = { targets = "not a list" } })
wait_for(function() return malformed_result ~= nil end, "malformed transport send completes")
vim.wait(30)
equals(malformed_callbacks, 1, "malformed transport callback count")
assert(not malformed_result.ok and malformed_result.error.code == "invalid_state", "malformed transport result is structured")
assert(vim.deep_equal(malformed_stages, { "list_targets" }), "malformed list never validates or delivers")
equals(#read_state(root_b).comments, 1, "malformed transport keeps drafts")

local old_root = state_root .. "/old-format"
vim.fn.mkdir(old_root .. "/.git", "p")
old_root = assert(vim.uv.fs_realpath(old_root))
vim.fn.mkdir(vim.fn.stdpath("state") .. "/herdr-review", "p")
vim.fn.writefile({ vim.json.encode({ version = 1, root = old_root, comments = {
  { file = "old.lua", start = 1, finish = 1, lines = "old source", text = "old draft" },
} }) }, state_file(old_root))
local old_records = call(function(done) return feedback.list({ review = review(old_root) }, done) end, "old-format list")
assert(old_records.ok and old_records.value[1].id and old_records.value[1].revision == 1, "old-format records remain readable and gain identity")
local old_envelope = read_state(old_root)
assert(old_envelope.version == 1 and old_envelope.root == old_root and old_envelope.comments[1].text == "old draft", "old-format envelope stays unchanged")

local deleted = call(function(done)
  return feedback.delete(captured_add.value.id, { review = review(root_a), expected_revision = 2 }, done)
end, "delete missing acknowledged record")
assert(not deleted.ok and deleted.error.code == "not_found", "ID operations require the complete review and report absence")

print("PR02 public API: documented add, shared legacy state, revisions, cancellation, and delivery: ok")
vim.cmd("qa!")
