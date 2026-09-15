local fixture = assert(vim.env.HERDR_TEST_FIXTURE_ROOT, "HERDR_TEST_FIXTURE_ROOT is required")
local repo = assert(vim.env.HERDR_TEST_REPO, "HERDR_TEST_REPO is required")
local state_root = assert(vim.env.HERDR_TEST_STATE_ROOT, "HERDR_TEST_STATE_ROOT is required")

assert(vim.fn.stdpath("state"):sub(1, #state_root) == state_root, "operations must use isolated state")
vim.opt.runtimepath:prepend(repo .. "/nvim")
vim.env.HERDR_WORKSPACE_ID = "fixture-workspace"
vim.env.HERDR_PANE_ID = "fixture-editor"
vim.env.HERDR_BIN_PATH = "FAKE-HERDR-OPERATIONS"

local notices, calls, pending = {}, {}, {}
vim.notify = function(message, level)
  table.insert(notices, { message = message, level = level })
end

local function assert_that(value, message)
  assert(value, message)
end

local function equals(actual, expected, message)
  assert(actual == expected, string.format("%s: expected %s, got %s", message, vim.inspect(expected), vim.inspect(actual)))
end

local function wait_for(predicate, message)
  assert_that(vim.wait(500, predicate, 5), message)
end

local function reset_fake()
  calls, pending, notices = {}, {}, {}
end

vim.system = function(argv, _, callback)
  table.insert(calls, vim.deepcopy(argv))
  if argv[2] == "agent" and argv[3] == "list" then
    pending.agents = callback
  elseif argv[2] == "tab" and argv[3] == "list" then
    pending.tabs = callback
  elseif argv[2] == "pane" and argv[3] == "send-text" then
    pending.bytes = argv[5]
    pending.send = callback
  elseif argv[2] == "agent" and argv[3] == "focus" then
    pending.focus = argv
  else
    error("unexpected fake command: " .. vim.inspect(argv))
  end
  return {}
end

local selected_items = {}
local picker_result
vim.ui.select = function(items, opts, callback)
  selected_items[opts.prompt or ""] = vim.deepcopy(items)
  callback(picker_result)
end

local state_dir = vim.fn.stdpath("state") .. "/herdr-review"
vim.fn.mkdir(state_dir, "p")
local function state_file(root)
  return state_dir .. "/" .. vim.fn.sha256(root):sub(1, 16) .. ".json"
end

local roots = {}
for _, name in ipairs({ "a", "b", "c" }) do
  local path = fixture .. "/operations-" .. name
  vim.fn.mkdir(path .. "/.git", "p")
  roots[name] = assert(vim.uv.fs_realpath(path))
  vim.fn.writefile({ "source " .. name, "second " .. name }, roots[name] .. "/example.lua")
end

local function record(id, text, revision)
  return { id = id, revision = revision or 1, file = "example.lua", start = 1, finish = 1, lines = "source fixture", text = text }
end

local ids = {
  one = "00000000-0000-4000-8000-000000000001",
  two = "00000000-0000-4000-8000-000000000002",
}

local function seed(root, comments)
  local path = state_file(root)
  if #comments == 0 then
    vim.fn.delete(path)
  else
    assert_that(vim.fn.writefile({ vim.json.encode({ version = 1, root = root, comments = comments }) }, path) == 0, "seed write")
  end
end

local function read(root)
  local path = state_file(root)
  if vim.fn.filereadable(path) == 0 then return nil end
  return vim.json.decode(table.concat(vim.fn.readfile(path), "\n"))
end

local function bytes(root)
  local path = state_file(root)
  return vim.fn.filereadable(path) == 1 and table.concat(vim.fn.readfile(path), "\n") or nil
end

local function reload_review()
  package.loaded.herdr_review = nil
  return require("herdr_review")
end

local function edit_root(root)
  vim.cmd("edit " .. vim.fn.fnameescape(root .. "/example.lua"))
  vim.api.nvim_win_set_cursor(0, { 1, 0 })
end

local function save_composer(text)
  local win = vim.api.nvim_get_current_win()
  vim.api.nvim_buf_set_lines(vim.api.nvim_win_get_buf(win), 0, -1, false, { text })
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<C-s>", true, false, true), "xt", false)
  return win
end

local function close_composer()
  local win = vim.api.nvim_get_current_win()
  if vim.api.nvim_win_get_config(win).relative ~= "" then vim.api.nvim_win_close(win, true) end
end

local function agents(rows)
  return { code = 0, stdout = vim.json.encode({ result = { agents = rows } }), stderr = "" }
end

local function one_agent()
  return agents({ { workspace_id = "fixture-workspace", pane_id = "fixture-agent", agent = "fixture", agent_status = "idle" } })
end

local function start_send(review, options)
  review.send(options)
  assert_that(pending.agents, "send must call public agent list")
end

local function choose_one()
  pending.agents(one_agent())
  wait_for(function() return pending.send ~= nil end, "agent discovery must register send callback")
end

local function ack(result)
  pending.send(result or { code = 0, stdout = "{}", stderr = "" })
  vim.wait(30)
end

local function list_count(review)
  picker_result = nil
  review.list()
  return #(selected_items["Review comments"] or {})
end

local function has_notice(fragment)
  for _, notice in ipairs(notices) do
    if notice.message:find(fragment, 1, true) then return true end
  end
  return false
end

local function temporary_files(root)
  return vim.fn.glob(state_file(root) .. ".tmp-*", false, true)
end

local function inject_commit_failure(root, stage)
  local destination = state_file(root)
  local real_writefile = vim.fn.writefile
  local real_write = vim.uv.fs_write
  local real_close = vim.uv.fs_close
  local real_rename = vim.uv.fs_rename
  vim.fn.writefile = function(lines, path, flags)
    if path == destination then
      if stage == "write" then real_writefile({ "partial" }, path) end
      return 1
    end
    return real_writefile(lines, path, flags)
  end
  vim.uv.fs_write = function(file, data, offset, callback)
    if not callback and stage == "write" then
      real_write(file, "partial", offset)
      return nil, "injected partial write", "EIO"
    end
    return real_write(file, data, offset, callback)
  end
  vim.uv.fs_close = function(file, callback)
    if not callback and stage == "close" then
      real_close(file)
      return nil, "injected close failure", "EIO"
    end
    return real_close(file, callback)
  end
  vim.uv.fs_rename = function(source, target, callback)
    if not callback and stage == "replace" and target == destination then
      return nil, "injected replacement failure", "EIO"
    end
    return real_rename(source, target, callback)
  end
  return function()
    vim.fn.writefile = real_writefile
    vim.uv.fs_write = real_write
    vim.uv.fs_close = real_close
    vim.uv.fs_rename = real_rename
  end
end

-- 1. A delayed A send only acknowledges its captured A revision after B becomes active.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "from A") })
  seed(roots.b, { record(ids.two, "from B") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review)
  edit_root(roots.b)
  review.list()
  choose_one()
  equals(pending.bytes, "\27[200~example.lua:1\nsource fixture\nfrom A\27[201~", "root-switch exact fake bytes")
  ack()
  equals(read(roots.a), nil, "root-switch clears A only")
  equals(read(roots.b).comments[1].text, "from B", "root-switch preserves B")
end

-- 2. A composer captures A before the user visits B.
do
  reset_fake()
  seed(roots.a, {})
  seed(roots.b, {})
  local review = reload_review()
  edit_root(roots.a)
  local source_win = vim.api.nvim_get_current_win()
  review.comment(false)
  local composer = vim.api.nvim_get_current_win()
  vim.api.nvim_set_current_win(source_win)
  edit_root(roots.b)
  review.list()
  vim.api.nvim_set_current_win(composer)
  save_composer("saved after B")
  wait_for(function() return read(roots.a) ~= nil end, "composer A must persist")
  equals(read(roots.a).comments[1].text, "saved after B", "composer writes captured A")
  equals(read(roots.b), nil, "composer does not write B")
end

-- 3. A draft created during a pending acknowledgement survives.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "captured") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review)
  choose_one()
  review.comment(false)
  save_composer("new while pending")
  wait_for(function() return #read(roots.a).comments == 2 end, "new draft must persist before acknowledgement")
  ack()
  local after = read(roots.a)
  equals(#after.comments, 1, "acknowledgement retains new draft")
  equals(after.comments[1].text, "new while pending", "acknowledgement retains exact new text")
end

-- 4. An edit after capture increments revision and is retained by acknowledgement.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "captured") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review)
  review.edit()
  save_composer("edited after capture")
  wait_for(function() return read(roots.a).comments[1].revision == 2 end, "edit must persist revision")
  choose_one()
  ack()
  local after = read(roots.a)
  equals(#after.comments, 1, "acknowledgement must retain changed revision")
  equals(after.comments[1].revision, 2, "retained revision")
  equals(after.comments[1].text, "edited after capture", "retained edited text")
end

-- 5. Cancelling the target picker emits no delivery bytes and keeps drafts.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "cancel me") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review)
  pending.agents(agents({
    { workspace_id = "fixture-workspace", pane_id = "agent-1", agent = "one" },
    { workspace_id = "fixture-workspace", pane_id = "agent-2", agent = "two" },
  }))
  wait_for(function() return pending.tabs ~= nil end, "multiple agents must load tabs")
  pending.tabs({ code = 0, stdout = '{"result":{"tabs":[]}}', stderr = "" })
  vim.wait(30)
  equals(pending.send, nil, "picker cancellation sends no bytes")
  equals(read(roots.a).comments[1].text, "cancel me", "picker cancellation keeps draft")
end

-- 6. A definite discovery failure sends no bytes and retains the draft.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "discovery failure") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review)
  pending.agents({ code = 1, stdout = "", stderr = "fixture unavailable" })
  vim.wait(30)
  equals(pending.send, nil, "discovery failure accepts no input")
  equals(read(roots.a).comments[1].text, "discovery failure", "discovery failure keeps draft")
  assert_that(has_notice("did not return the workspace agents"), "discovery failure is actionable")
end

-- 7. Bytes accepted followed by a nonzero acknowledgement is uncertain and never retried.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "uncertain bytes") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review)
  choose_one()
  local accepted = pending.bytes
  ack({ code = 1, stdout = "", stderr = "lost acknowledgement" })
  equals(pending.bytes, accepted, "uncertain send performs one input attempt")
  equals(read(roots.a).comments[1].text, "uncertain bytes", "uncertain send retains draft")
  assert_that(has_notice("Delivery outcome is uncertain"), "uncertain outcome is explicit")
end

-- 8. Closing the source window does not steal focus or lose the captured composer draft.
do
  reset_fake()
  seed(roots.a, {})
  local review = reload_review()
  edit_root(roots.b)
  vim.cmd("split " .. vim.fn.fnameescape(roots.a .. "/example.lua"))
  local source_win = vim.api.nvim_get_current_win()
  review.comment(false)
  local composer = vim.api.nvim_get_current_win()
  vim.api.nvim_win_close(source_win, true)
  vim.api.nvim_set_current_win(composer)
  save_composer("saved after source close")
  wait_for(function() return read(roots.a) ~= nil end, "closed-origin composer must persist")
  equals(read(roots.a).comments[1].text, "saved after source close", "closed-origin exact draft")
  equals(vim.api.nvim_buf_get_name(vim.api.nvim_get_current_buf()), roots.b .. "/example.lua", "closed-origin leaves unrelated B focused")
end

-- 9. Legacy records receive durable UUIDv4 and positive revisions that survive module restart.
do
  reset_fake()
  seed(roots.a, { { file = "example.lua", start = 1, finish = 1, lines = "source fixture", text = "legacy" } })
  local review = reload_review()
  edit_root(roots.a)
  equals(list_count(review), 1, "legacy list uses public operation")
  local before = read(roots.a).comments[1]
  assert_that(before.id:match("^%x%x%x%x%x%x%x%x%-%x%x%x%x%-4%x%x%x%-[89ab]%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$"), "legacy ID must be UUIDv4")
  assert_that(type(before.revision) == "number" and before.revision >= 1 and before.revision % 1 == 0, "legacy revision must be positive integer")
  review = reload_review()
  edit_root(roots.a)
  equals(list_count(review), 1, "restart public list sees durable record")
  local after = read(roots.a).comments[1]
  equals(after.id, before.id, "restart keeps normalized UUID")
  equals(after.revision, before.revision, "restart keeps normalized revision")
end

-- 10. A selected batch acknowledges exactly the requested IDs and exact fake bytes.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "first"), record(ids.two, "second") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review, { annotation_ids = { ids.two } })
  choose_one()
  equals(pending.bytes, "\27[200~example.lua:1\nsource fixture\nsecond\27[201~", "selected batch exact fake bytes")
  ack()
  local after = read(roots.a)
  equals(#after.comments, 1, "selected batch leaves unselected draft")
  equals(after.comments[1].id, ids.one, "selected batch leaves exact unselected ID")
end

-- Durable failure cases: pre-commit write, close, and replacement failures
-- must not change the committed state or discard editable text.
for _, stage in ipairs({ "write", "close", "replace" }) do
  reset_fake()
  seed(roots.a, { record(ids.one, "durable") })
  local review = reload_review()
  edit_root(roots.a)
  local original = bytes(roots.a)
  local restore = inject_commit_failure(roots.a, stage)
  review.comment(false)
  local composer = save_composer(stage .. " must fail")
  vim.wait(30)
  equals(bytes(roots.a), original, stage .. " failure preserves literal bytes")
  assert_that(vim.api.nvim_win_is_valid(composer), stage .. " failure retains composer")
  equals(#temporary_files(roots.a), 0, stage .. " failure cleans owned temporary file")
  restore()
  close_composer()
end

do
  reset_fake()
  seed(roots.a, { record(ids.one, "durable") })
  local review = reload_review()
  edit_root(roots.a)
  local original = bytes(roots.a)
  local real_delete = vim.fn.delete
  local real_unlink = vim.uv.fs_unlink
  vim.fn.delete = function(path, flags)
    if path == state_file(roots.a) then return 1 end
    return real_delete(path, flags)
  end
  vim.uv.fs_unlink = function(path, callback)
    if path == state_file(roots.a) then return nil, "injected delete failure", "EIO" end
    return real_unlink(path, callback)
  end
  review.delete()
  vim.wait(30)
  equals(bytes(roots.a), original, "delete failure preserves literal bytes")
  equals(list_count(review), 1, "delete failure preserves memory")
  vim.fn.delete = real_delete
  vim.uv.fs_unlink = real_unlink
end

-- Accepted input is still uncertain when acknowledgement persistence fails.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "ack delete failure") })
  local review = reload_review()
  edit_root(roots.a)
  start_send(review)
  choose_one()
  local original = bytes(roots.a)
  local real_delete = vim.fn.delete
  local real_unlink = vim.uv.fs_unlink
  vim.fn.delete = function(path, flags)
    if path == state_file(roots.a) then return 1 end
    return real_delete(path, flags)
  end
  vim.uv.fs_unlink = function(path, callback)
    if path == state_file(roots.a) then return nil, "injected delete failure", "EIO" end
    return real_unlink(path, callback)
  end
  ack()
  vim.fn.delete = real_delete
  vim.uv.fs_unlink = real_unlink
  equals(bytes(roots.a), original, "ack delete failure preserves literal drafts")
  assert_that(has_notice("acknowledgement could not be saved"), "ack delete failure is uncertain")

  reset_fake()
  seed(roots.a, { record(ids.one, "ack write failure"), record(ids.two, "keep me") })
  review = reload_review()
  edit_root(roots.a)
  start_send(review, { annotation_ids = { ids.one } })
  choose_one()
  original = bytes(roots.a)
  local restore = inject_commit_failure(roots.a, "write")
  edit_root(roots.b)
  review.list()
  ack()
  restore()
  equals(bytes(roots.a), original, "ack write failure preserves literal drafts")
  equals(#read(roots.a).comments, 2, "inactive origin retains selected and unselected drafts")
  assert_that(has_notice("acknowledgement could not be saved"), "ack write failure is uncertain")
end

-- A conflicting edit remains in its composer until a matching revision can persist.
do
  reset_fake()
  seed(roots.a, { record(ids.one, "before conflict") })
  local review = reload_review()
  edit_root(roots.a)
  local source_win = vim.api.nvim_get_current_win()
  review.edit()
  local first_composer = vim.api.nvim_get_current_win()
  vim.api.nvim_set_current_win(source_win)
  review.edit()
  save_composer("external edit")
  wait_for(function() return read(roots.a).comments[1].revision == 2 end, "external edit persists newer revision")
  vim.api.nvim_set_current_win(first_composer)
  save_composer("stale composer edit")
  vim.wait(30)
  assert_that(vim.api.nvim_win_is_valid(first_composer), "conflicting edit retains composer")
  equals(read(roots.a).comments[1].text, "external edit", "conflict preserves durable newer text")
  assert_that(has_notice("Comment changed while editing"), "conflict is actionable")
  close_composer()
end

-- Invalid records are read-only: public mutation leaves their exact bytes untouched.
for _, bad in ipairs({
  { label = "malformed", body = "{not json" },
  { label = "wrong-root", body = vim.json.encode({ version = 1, root = roots.b, comments = {} }) },
}) do
  reset_fake()
  vim.fn.writefile({ bad.body }, state_file(roots.c))
  local review = reload_review()
  edit_root(roots.c)
  review.comment(false)
  vim.wait(30)
  equals(bytes(roots.c), bad.body, bad.label .. " state must not be overwritten")
  assert_that(has_notice("review state"), bad.label .. " state reports actionable error")
end

print("PR01 operations: 10 async lanes plus durable failure and recovery cases: ok")
vim.cmd("qa!")
